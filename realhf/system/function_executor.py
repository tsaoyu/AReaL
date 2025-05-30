# Copyright 2025 Ant Group Inc.
import asyncio
import random
from typing import *

import networkx as nx
from tensorboardX import SummaryWriter

from realhf.api.core.config import ModelName, ModelShardID
from realhf.api.core.data_api import DataBatchMeta, SequenceSample
from realhf.api.core.dfg import MFCDef
from realhf.api.core.model_api import ReaLModelConfig
from realhf.base import logging
from realhf.base.topology import ProcessTopology
from realhf.system.buffer import AsyncIOSequenceBuffer
from realhf.system.model_function_call import ModelFunctionCall, RPCCorountineControl
from realhf.system.redistributor import GlobalStorageTracker, RedistribPlanner
from realhf.system.request_reply_stream import NameResolvingRequestClient

logger = logging.getLogger(__name__, "system")
blogger = logging.getLogger("benchmark")


class FunctionExecutor:
    def __init__(
        self,
        rpcs: List[MFCDef],
        msid2mwid: Dict[ModelShardID, int],
        stream: NameResolvingRequestClient,
        buffer: AsyncIOSequenceBuffer,
        model_topos: Dict[str, ProcessTopology],
        model_configs: Dict[str, None | ReaLModelConfig],
        ctrl: RPCCorountineControl,
        summary_writer: SummaryWriter | None,
        shuffle_dataset: bool,
    ):

        self.func_calls: Dict[str, ModelFunctionCall] = {}
        self.ctrl = ctrl

        self.n_model_workers = len(set(msid2mwid.values()))
        self.msid2mwid = msid2mwid

        self.storage_tracker = GlobalStorageTracker(self.n_model_workers)
        self.redistrib_planner = RedistribPlanner(self.storage_tracker)

        self.rpcs = rpcs
        self.src_rpc = list(filter(lambda rpc: rpc.is_src, rpcs))[0]
        self.src_dp_size = model_topos[self.src_rpc.model_name].get_dim("data")

        # Create model function calls.
        for rpc in self.rpcs:
            func_call = ModelFunctionCall(
                rpc=rpc,
                src_rpc=self.src_rpc,
                stream=stream,
                msid2mwid=msid2mwid,
                model_topos=model_topos,
                model_configs=model_configs,
                ctrl=ctrl,
                buffer=buffer,
                redistrib_planner=self.redistrib_planner,
                summary_writer=summary_writer,
            )
            self.func_calls[rpc.name] = func_call

        self.stream = stream
        self.buffer = buffer

        self.data_loading_dp_idx = -1
        self.shuffle_dataset = shuffle_dataset

        # Sort all MFCs in the topological order and
        # calculate the width of each level.
        # These numbers will determine when to flush MFC requests.
        self.topo_widths = []
        for generation in nx.topological_generations(rpcs[0]._G):
            self.topo_widths.append(len(generation))

    def get_leaf_tasks(self) -> List[str]:
        dst_rpcs = list(filter(lambda rpc: rpc.is_dst, self.rpcs))
        return [rpc.name for rpc in dst_rpcs]

    async def flush_calls(self):
        for level, w in enumerate(self.topo_widths):
            for _ in range(w):
                await self.ctrl.topo_level_count.get()
            logger.info(f"DFG level {level}. Flushing {w} function calls.")
            self.stream.request(
                handlers=list(range(self.n_model_workers)), handle_type="flush"
            )

    async def finish_traverse(self):
        for _ in range(len(self.get_leaf_tasks())):
            await self.ctrl.train_count.get()
        await self.clear_gpu_cache()

    async def clear_gpu_cache(self):
        async with self.ctrl.lock:
            self.ctrl.used_hash_vals_this_epoch += list(self.ctrl.ids_to_clear)
            self.stream.request(
                handlers=list(range(self.n_model_workers)),
                handle_type="clear_data_cache",
                datas=[
                    self.ctrl.ids_to_clear for _ in list(range(self.n_model_workers))
                ],
                no_syn=True,
            )
            # Clear resource tracker as well.
            await self.storage_tracker.clear_data(self.ctrl.ids_to_clear)

            self.ctrl.ids_to_clear.clear()

    async def load_data(self):
        buffer = self.buffer
        ctrl = self.ctrl

        received_ids = set()

        while self.buffer.size < max(rpc.n_seqs for rpc in self.rpcs):

            resps = await self.stream.call_async(
                handlers=[f"__data{dp_idx}__" for dp_idx in range(self.src_dp_size)],
                handle_type="fetch",
                datas=[None for _ in range(self.src_dp_size)],
                verbose=False,
            )

            all_data = []
            data_cnt = []
            gpu_id_data = {}
            for dp_rank, x in enumerate(resps):
                x: DataBatchMeta | None

                if x is None:
                    data_cnt.append(0)
                    continue
                if x.meta_sample is None:
                    data_cnt.append(0)
                    continue

                for xx in x.meta_sample.unpack():
                    async with ctrl.lock:
                        if xx.ids[0] in received_ids:
                            raise ValueError(f"Duplicate data id {xx.ids[0]}.")
                        received_ids.add(xx.ids[0])

                gpu_id = self.stream.route_to(f"__data{dp_rank}__")
                all_data += x.meta_sample.unpack()
                gpu_id_data[gpu_id] = x.meta_sample.unpack()
                data_cnt.append(x.meta_sample.bs)

            if self.shuffle_dataset:
                # We load data in a round-robin manner across different DP ranks,
                # so we also need to shuffle the data to fuse different dataset splits.
                random.shuffle(all_data)

            if len(all_data) > 0:
                # Update resource tracker for planning data redistribution.
                for gpu_id, data in gpu_id_data.items():
                    for k in data[0].keys:
                        await self.storage_tracker.add_data(
                            gpu_id,
                            [d.ids[0] for d in data],
                            k,
                            is_owner=True,
                        )

                # Store into buffer!
                buffer_indices = await buffer.put_batch(all_data)
                assert len(buffer_indices) == len(all_data)

                blogger.info(
                    f"Master worker loaded {len(all_data)} pieces of data from all dp ranks: "
                    f"{data_cnt} from each rank. "
                    f"Current buffer size: {buffer.size}/{buffer.max_size}. "
                )
            else:
                await asyncio.sleep(1)

    def execute_step(self):
        logger.info("Waiting for the finish of the execution graph.")
        loop = asyncio.get_event_loop()

        tasks = [loop.create_task(fc.run()) for fc in self.func_calls.values()] + [
            loop.create_task(self.flush_calls()),
            loop.create_task(self.load_data()),
            loop.create_task(self.finish_traverse()),
        ]

        loop.run_until_complete(asyncio.gather(*tasks))
