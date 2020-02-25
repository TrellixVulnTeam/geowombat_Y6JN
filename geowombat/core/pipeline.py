from abc import ABC, abstractmethod
from contextlib import ExitStack
from pathlib import Path
from copy import copy

from ..errors import logger
from ..radiometry import BRDF, LinearAdjustments, RadTransforms

import geowombat as gw
import xarray as xr
import graphviz


rt = RadTransforms()
br = BRDF()
la = LinearAdjustments()

PROC_NODE_ATTRS = {
    "shape": "oval",
    "color": "#3454b4",
    "fontcolor": "#131f43",
    "style": "filled",
    "fillcolor": "#c6d2f6"}

PROC_EDGE_ATTRS = {"color": "#3454b4", "style": "bold"}

CONFIG_NODE_ATTRS = {
    "shape": "diamond",
    "color": "black",
    "fontcolor": "#131f43",
    "style": "rounded,filled",
    "fillcolor": "none"}

CONFIG_EDGE_ATTRS = {"color": "grey", "style": "dashed"}

OUT_NODE_ATTRS = {
    "shape": "pentagon",
    "color": "black",
    "fontcolor": "#131f43",
    "style": "rounded,filled",
    "fillcolor": "none"}

OUT_EDGE_ATTRS = {"color": "grey", "style": "dashed"}

INPUT_NODE_ATTRS = {
    "shape": "box",
    "color": "#b49434",
    "fontcolor": "#2d250d",
    "style": "filled",
    "fillcolor": "#f3e3b3"}

INPUT_EDGE_ATTRS = {"color": "#b49434"}

VAR_NODE_ATTRS = {"shape": "box", "color": "#555555", "fontcolor": "#555555", "style": "dashed"}

VAR_EDGE_ATTRS = {"color": "#555555"}


class BaseGeoTasks(ABC):

    @abstractmethod
    def __init__(self,
                 inputs,
                 outputs,
                 tasks,
                 clean,
                 config_args=None,
                 open_args=None,
                 func_args=None,
                 out_args=None):

        self.inputs = inputs
        self.outputs = outputs
        self.tasks = tasks
        self.clean = clean
        self.config_args = config_args if inputs else {}
        self.open_args = open_args if inputs else {}
        self.func_args = func_args if inputs else {}
        self.out_args = out_args if inputs else {}

    def copy(self):
        return copy(self)

    def __add__(self, other):

        """Add another pipeline"""

        self_inputs_copy = self.inputs.copy()
        self_outputs_copy = self.outputs.copy()
        self_func_args_copy = self.func_args.copy()
        self_config_args_copy = self.config_args.copy()
        self_open_args_copy = self.open_args.copy()
        self_out_args_copy = self.out_args.copy()

        tasks = list()
        clean = list()

        for task_id, task in self.tasks:
            tasks.append((task_id, task))

        for task_id, task in other.tasks:
            tasks.append((task_id, task))

        for task_id in self.clean:
            clean.append(task_id)

        for task_id in other.clean:
            clean.append(task_id)

        self_inputs_copy.update(other.inputs)
        self_outputs_copy.update(other.outputs)
        self_func_args_copy.update(other.func_args)
        self_config_args_copy.update(other.config_args)
        self_open_args_copy.update(other.open_args)
        self_out_args_copy.update(other.out_args)

        return GeoTasks(self_inputs_copy,
                        self_outputs_copy,
                        tuple(tasks),
                        tuple(clean),
                        config_args=self_config_args_copy,
                        open_args=self_open_args_copy,
                        func_args=self_func_args_copy,
                        out_args=self_out_args_copy)

    @abstractmethod
    def execute(self, task_id, task, src, **kwargs):
        """Execute a task"""
        pass

    @abstractmethod
    def submit(self):
        """Submit a task pipeline"""
        raise NotImplementedError

    @abstractmethod
    def cleanup(self):
        """Cleanup task outputs"""
        pass

    def _validate_methods(self, *args):

        if len(args) != len(self.processes):
            raise AttributeError('The lengths do not match.')

        for object_, proc_ in zip(*args, self.processes):

            if not hasattr(object_, proc_):
                raise NameError(f'The {proc_} process is not supported.')

    def __len__(self):
        return len(self.processes)


class GraphBuilder(object):

    """
    Reference:
        https://github.com/benbovy/xarray-simlab/blob/master/xsimlab/dot.py
    """

    def visualize(self):

        self.seen = set()
        self.inputs_seen = set()
        self.outputs_seen = set()

        counter = 0

        self.g = graphviz.Digraph()
        self.g.subgraph(graph_attr={'rankdir': 'LR'})

        for task_id, task in self.tasks:

            if task_id not in self.seen:

                self.seen.add(task_id)
                self.g.node(task_id, label=f'Task {task_id}: {task.__name__}', **PROC_NODE_ATTRS)

            if task_id != list(self.tasks)[0][0]:
                self.g.edge(list(self.tasks)[counter - 1][0], task_id, **PROC_EDGE_ATTRS)

            for config_key, config_setting in self.config_args.items():

                self.g.node(config_key, label=f'{config_key}: {config_setting}', **CONFIG_NODE_ATTRS)
                self.g.edge(config_key, list(self.tasks)[counter - 1][0], **CONFIG_EDGE_ATTRS)

            counter += 1

        counter = 0

        for task_id, output_ in self.outputs.items():

            if output_ not in self.outputs_seen:
                self.outputs_seen.add(output_)

            node_attrs = INPUT_NODE_ATTRS.copy()
            edge_attrs = INPUT_EDGE_ATTRS.copy()

            if task_id in self.clean:
                node_attrs['color'] = 'red'

            if task_id == list(self.outputs.keys())[-1]:
                node_attrs['color'] = 'blue'

            node_attrs['style'] = 'dashed'
            edge_attrs['style'] = 'dashed'

            self.g.node(f'{task_id} {self.outputs[task_id]}', label=self.outputs[task_id], **node_attrs)
            self.g.edge(task_id, f'{task_id} {self.outputs[task_id]}', weight='200', **edge_attrs)

            for out_key, out_setting in self.out_args.items():

                self.g.node(out_key, label=f'{out_key}: {out_setting}', **OUT_NODE_ATTRS)
                self.g.edge(out_key, f'{task_id} {self.outputs[task_id]}', **OUT_EDGE_ATTRS)

            if counter > 0:

                task_id_ = list(self.outputs.keys())[counter-1]
                self.g.edge(f'{task_id_} {self.outputs[task_id_]}', task_id, weight='200', **edge_attrs)

            counter += 1

        counter = 0

        for task_id, inputs_ in self.inputs.items():

            if isinstance(inputs_, str):
                self._add_inputs(counter, task_id, [inputs_])
            else:
                self._add_inputs(counter, task_id, inputs_)

                counter += 1

        for task_id, params in self.func_args.items():

            for k, param in params.items():

                self.g.node(f'{task_id} {k}', label=f'{k}: {param}', **VAR_NODE_ATTRS)
                self.g.edge(f'{task_id} {k}', task_id, weight='200', **VAR_EDGE_ATTRS)

        return self.g

    def _add_inputs(self, counter, task_id, input_list):

        for input_ in input_list:

            if input_ in self.outputs_seen:
                task_id_b = None
            else:

                if input_ not in self.inputs_seen:

                    self.inputs_seen.add(input_)
                    self.g.node(f'{task_id} {input_}', label=input_, **INPUT_NODE_ATTRS)
                    task_id_b = task_id

                else:
                    task_id_b = list(self.tasks)[0][counter-1]

            if task_id_b:
                self.g.edge(f'{task_id_b} {input_}', task_id, weight='200', **INPUT_EDGE_ATTRS)


class GeoTasks(BaseGeoTasks, GraphBuilder):

    """
    Example:
        >>> import geowombat as gw
        >>> from geowombat.core import pipeline
        >>> from geowombat.radiometry import RadTransforms
        >>> rt = RadTransforms()
        >>>
        >>> tasks = (('A', rt.dn_to_sr), ('B', gw.ndvi))
        >>> clean = ('A')
        >>>
        >>> inputs = {'A': ('input.tif', 'sza.tif', 'saa.tif', 'vza.tif', 'vaa.tif'),
        >>>           'B': 'A'}
        >>>
        >>> # {'task': (func, output)}
        >>> outputs = {'A': 'sr.tif',
        >>>            'B': 'ndvi.tif'}
        >>>
        >>> func_args = {'A': {'meta': 'meta.mtl'}}
        >>>
        >>> open_args = {'chunks': 512}
        >>> config_args = {'sensor': 'l7', 'scale_factor': 0.0001}
        >>> out_args = {'compress': 'lzw', 'overwrite': True}
        >>>
        >>> task = pipeline.GeoTasks(inputs, outputs, tasks, clean, config_args, open_args, func_args, out_args)
        >>>
        >>> task.visualize()
        >>> task.submit()
        >>>
        >>> # Add pipelines
        >>> task_sum = pipeline.GeoTasks(...)
        >>> task = task + task_sum
    """

    def __init__(self,
                 inputs,
                 outputs,
                 tasks,
                 clean,
                 config_args=None,
                 open_args=None,
                 func_args=None,
                 out_args=None):

        super().__init__(inputs,
                         outputs,
                         tasks,
                         clean,
                         config_args,
                         open_args,
                         func_args,
                         out_args)

    def execute(self, task_id, task, src, **kwargs):

        # Execute the task
        res = task(*src, **kwargs)

        # Write to file, if needed
        # TODO: how to handle in-memory results
        if task_id in self.outputs:
            res.gw.to_raster(self.outputs[task_id], **self.out_args)

    def submit(self):

        with gw.config.update(**self.config_args):

            for task_id, task in self.tasks:

                # Check task keywords
                kwargs = self.func_args[task_id] if task_id in self.func_args else {}

                # Check task input(s)
                if isinstance(self.inputs[task_id], str) and not Path(self.inputs[task_id]).is_file():

                    with gw.open(self.outputs[self.inputs[task_id]]) as src:
                        self.execute(task_id, task, src, **kwargs)

                if isinstance(self.inputs[task_id], str) and Path(self.inputs[task_id]).is_file():

                    with gw.open(self.inputs[task_id], **self.open_args) as src:
                        self.execute(task_id, task, src, **kwargs)

                else:

                    with ExitStack() as stack:

                        # Open input files for the task
                        src = [stack.enter_context(gw.open(fn, **self.open_args)) for fn in self.inputs[task_id]]
                        self.execute(task_id, task, src, **kwargs)

        self.cleanup()

    def cleanup(self):

        for task_id in self.clean:

            fn = Path(self.outputs[task_id])

            if fn.is_file():

                try:
                    fn.unlink()
                except:
                    logger.warning(f'  Could not remove task {task_id} output.')



# class LandsatBRDFPipeline(GeoPipeline):
#
#     """
#     A pipeline class for Landsat BRDF
#
#     Args:
#         processes (tuple): The spectral indices to process.
#
#     Returns:
#         ``xarray.DataArray``
#
#     Example:
#         >>> import geowombat as gw
#         >>> from geowombat.core import pipeline
#         >>> from geowombat.radiometry import RadTransforms
#         >>>
#         >>> rt = RadTransforms()
#         >>> meta = rt.get_landsat_coefficients('file.MTL')
#         >>>
#         >>> task = pipeline.LandsatBRDFPipeline(('dn_to_sr', 'norm_brdf', 'bandpass'))
#         >>>
#         >>> with gw.open('image.tif') as src, \
#         >>>     gw.open('sza.tif') as sza, \
#         >>>         gw.open('saa.tif') as saa, \
#         >>>             gw.open('vza.tif') as vza, \
#         >>>                 gw.open('vaa.tif') as vaa:
#         >>>
#         >>>     res = task.submit(src, sza, saa, vza, vaa, sensor=meta.sensor, meta=meta)
#     """
#
#     def __init__(self, processes):
#
#         super().__init__(processes)
#         self._validate_methods([rt, br, la])
#
#     def submit(self, data, *args, **kwargs):
#
#         for i, func_ in enumerate(self.processes):
#
#             if func_ == 'dn_to_sr':
#
#                 func = getattr(rt, func_)
#
#                 if i == 0:
#
#                     res = func(data,
#                                *args,
#                                sensor=kwargs['sensor'],
#                                meta=kwargs['meta'])
#
#                 else:
#
#                     res = func(res,
#                                *args,
#                                sensor=kwargs['sensor'],
#                                meta=kwargs['meta'])
#
#             elif func_ == 'norm_brdf':
#
#                 func = getattr(br, func_)
#
#                 if i == 0:
#
#                     res = func(data,
#                                *args,
#                                sensor=kwargs['sensor'],
#                                wavelengths=data.band.values.tolist(),
#                                out_range=10000.0,
#                                nodata=65535)
#
#                 else:
#
#                     res = func(res,
#                                *args,
#                                sensor=kwargs['sensor'],
#                                wavelengths=data.band.values.tolist(),
#                                out_range=10000.0,
#                                nodata=65535)
#
#             elif func_ == 'bandpass':
#
#                 func = getattr(la, func_)
#
#                 if i == 0:
#
#                     if kwargs['sensor'].lower() in ['l5', 'l7']:
#
#                         res = func(data,
#                                    kwargs['sensor'].lower(),
#                                    to='l8',
#                                    scale_factor=0.0001)
#
#                     else:
#
#                         res = func(res,
#                                    kwargs['sensor'].lower(),
#                                    to='l8',
#                                    scale_factor=0.0001)
#
#         return res
#
#
# class IndicesPipeline(GeoPipeline):
#
#     """
#     A pipeline class for spectral indices
#
#     Args:
#         processes (tuple): The spectral indices to process.
#
#     Returns:
#         ``xarray.DataArray``
#
#     Example:
#         >>> import geowombat as gw
#         >>> from geowombat.core import pipeline
#         >>>
#         >>> task = pipeline.IndicesPipeline(('avi', 'evi2', 'evi', 'nbr', 'ndvi', 'tasseled_cap'))
#         >>>
#         >>> with gw.open('image.tif') as src:
#         >>>     res = task.submit(src, scale_factor=0.0001)
#     """
#
#     def __init__(self, processes):
#
#         super().__init__(processes)
#         self._validate_methods([gw]*len(processes))
#
#     def submit(self, data, *args, **kwargs):
#
#         attrs = data.attrs.copy()
#         results = []
#
#         for vi in self.processes:
#
#             vi_func = getattr(gw, vi)
#             results.append(vi_func(data, *args, **kwargs))
#
#         results = xr.concat(results, dim='band').astype('float64')
#
#         return results.assign_attrs(**attrs)
