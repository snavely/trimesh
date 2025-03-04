import copy
import time
import collections

import numpy as np

from .. import util
from .. import caching
from .. import exceptions
from .. import transformations

try:
    import networkx as nx
    _ForestParent = nx.DiGraph
except BaseException as E:
    # create a dummy module which will raise the ImportError
    # or other exception only when someone tries to use networkx
    nx = exceptions.ExceptionModule(E)
    _ForestParent = object


class TransformForest(object):
    def __init__(self, base_frame='world'):
        # a graph structure, subclass of networkx DiGraph
        self.transforms = EnforcedForest()
        # hashable, the base or root frame
        self.base_frame = base_frame

        # save paths, keyed with tuple (from, to)
        self._paths = {}
        # cache transformation matrices keyed with tuples
        self._updated = str(np.random.random())
        self._cache = caching.Cache(self.md5)

    def update(self, frame_to, frame_from=None, **kwargs):
        """
        Update a transform in the tree.

        Parameters
        ------------
        frame_from : hashable object
          Usually a string (eg 'world').
          If left as None it will be set to self.base_frame
        frame_to :  hashable object
          Usually a string (eg 'mesh_0')
        matrix : (4,4) float
          Homogeneous transformation matrix
        quaternion :  (4,) float
          Quaternion ordered [w, x, y, z]
        axis : (3,) float
          Axis of rotation
        angle :  float
          Angle of rotation, in radians
        translation : (3,) float
          Distance to translate
        geometry : hashable
          Geometry object name, e.g. 'mesh_0'
        """

        self._updated = str(np.random.random())
        self._cache.clear()

        # if no frame specified, use base frame
        if frame_from is None:
            frame_from = self.base_frame
        # convert various kwargs to a single matrix
        matrix = kwargs_to_matrix(**kwargs)

        # create the edge attributes
        attr = {'matrix': matrix, 'time': time.time()}
        # pass through geometry to edge attribute
        if 'geometry' in kwargs:
            attr['geometry'] = kwargs['geometry']

        # add the edges
        changed = self.transforms.add_edge(frame_from,
                                           frame_to,
                                           **attr)
        # set the node attribute with the geometry information
        if 'geometry' in kwargs:
            nx.set_node_attributes(
                self.transforms,
                name='geometry',
                values={frame_to: kwargs['geometry']})
        # if the edge update changed our structure
        # dump our cache of shortest paths
        if changed:
            self._paths = {}

    def md5(self):
        return self._updated

    def copy(self):
        """
        Return a copy of the current TransformForest

        Returns
        ------------
        copied: TransformForest
        """
        copied = TransformForest()
        copied.base_frame = copy.deepcopy(self.base_frame)
        copied.transforms = copy.deepcopy(self.transforms)

        return copied

    def to_flattened(self, base_frame=None):
        """
        Export the current transform graph as a flattened
        """
        if base_frame is None:
            base_frame = self.base_frame

        flat = {}
        for node in self.nodes:
            if node == base_frame:
                continue
            transform, geometry = self.get(
                frame_to=node, frame_from=base_frame)
            flat[node] = {
                'transform': transform.tolist(),
                'geometry': geometry
            }
        return flat

    def to_gltf(self, scene):
        """
        Export a transforms as the 'nodes' section of a GLTF dict.
        Flattens tree.

        Returns
        --------
        gltf : dict
          with 'nodes' referencing a list of dicts
        """
        # geometry is an OrderedDict
        # {geometry key : index}
        mesh_index = {name: i for i, name
                      in enumerate(scene.geometry.keys())}
        # save the output
        gltf = collections.deque([])
        # only export nodes which have geometry
        for node in self.nodes_geometry:
            # don't include edge for base frame
            if node == self.base_frame:
                continue
            # get the transform and geometry from the graph
            transform, geometry = self.get(
                frame_to=node, frame_from=self.base_frame)
            # add a node by name
            gltf.append({'name': node})
            # if the transform is an identity matrix don't include it
            is_identity = np.abs(transform - np.eye(4)).max() < 1e-5
            if not is_identity:
                gltf[-1]['matrix'] = transform.T.reshape(-1).tolist()
            # assign geometry if it exists
            if geometry is not None:
                gltf[-1]['mesh'] = mesh_index[geometry]
            # check to see if we have camera node
            if node == scene.camera.name:
                gltf[-1]['camera'] = 0

        # we have flattened tree, so all nodes will be child of world
        gltf.appendleft({
            'name': self.base_frame,
            'children': list(range(1, 1 + len(gltf)))
        })
        result = {'nodes': list(gltf)}

        return result

    def to_edgelist(self):
        """
        Export the current transforms as a list of edge tuples, with
        each tuple having the format:
        (node_a, node_b, {metadata})

        Returns
        ---------
        edgelist: (n,) list of tuples
        """
        # save cleaned edges
        export = []
        # loop through (node, node, edge attributes)
        for edge in nx.to_edgelist(self.transforms):
            a, b, c = edge
            # geometry is a node property but save it to the
            # edge so we don't need two dictionaries
            if 'geometry' in self.transforms.node[b]:
                c['geometry'] = self.transforms.node[b]['geometry']
            # save the matrix as a float list
            c['matrix'] = np.asanyarray(c['matrix'], dtype=np.float64).tolist()
            export.append((a, b, c))
        return export

    def from_edgelist(self, edges, strict=True):
        """
        Load transform data from an edge list into the current
        scene graph.

        Parameters
        -------------
        edgelist : (n,) tuples
            (node_a, node_b, {key: value})
        strict : bool
            If true, raise a ValueError when a
            malformed edge is passed in a tuple.
        """
        # loop through each edge
        for edge in edges:
            # edge contains attributes
            if len(edge) == 3:
                self.update(edge[1], edge[0], **edge[2])
            # edge just contains nodes
            elif len(edge) == 2:
                self.update(edge[1], edge[0])
            # edge is broken
            elif strict:
                raise ValueError('edge incorrect shape: {}'.format(str(edge)))

    def load(self, edgelist):
        """
        Load transform data from an edge list into the current
        scene graph.

        Parameters
        -------------
        edgelist : (n,) tuples
            (node_a, node_b, {key: value})
        """
        self.from_edgelist(edgelist, strict=True)

    @caching.cache_decorator
    def nodes(self):
        """
        A list of every node in the graph.

        Returns
        -------------
        nodes: (n,) array, of node names
        """
        nodes = np.array(list(self.transforms.nodes()))
        return nodes

    @caching.cache_decorator
    def nodes_geometry(self):
        """
        The nodes in the scene graph with geometry attached.

        Returns
        ------------
        nodes_geometry: (m,) array, of node names
        """

        nodes = np.array([
            n for n in self.transforms.nodes()
            if 'geometry' in self.transforms.node[n]])

        return nodes

    def get(self, frame_to, frame_from=None):
        """
        Get the transform from one frame to another, assuming they are connected
        in the transform tree.

        If the frames are not connected a NetworkXNoPath error will be raised.

        Parameters
        ------------
        frame_to : hashable
          Node name, usually a string (eg 'mesh_0')
        frame_from : hashable
          Node name, usually a string (eg 'world').
          If None it will be set to self.base_frame

        Returns
        ----------
        transform : (4, 4) float
          Homogeneous transformation matrix
        """

        if frame_from is None:
            frame_from = self.base_frame

        # look up transform to see if we have it already
        cache_key = (frame_from, frame_to)
        cached = self._cache[cache_key]
        if cached is not None:
            return cached

        # get the path in the graph
        path = self._get_path(frame_from, frame_to)

        # collect transforms along the path
        transforms = []

        for i in range(len(path) - 1):
            # get the matrix and edge direction
            data, direction = self.transforms.get_edge_data_direction(
                path[i], path[i + 1])
            matrix = data['matrix']
            if direction < 0:
                matrix = np.linalg.inv(matrix)
            transforms.append(matrix)
        # do all dot products at the end
        if len(transforms) == 0:
            transform = np.eye(4)
        elif len(transforms) == 1:
            transform = np.asanyarray(transforms[0], dtype=np.float64)
        else:
            transform = util.multi_dot(transforms)

        geometry = None
        if 'geometry' in self.transforms.node[frame_to]:
            geometry = self.transforms.node[frame_to]['geometry']

        self._cache[cache_key] = (transform, geometry)

        return transform, geometry

    def show(self):
        """
        Plot the graph layout of the scene.
        """
        import matplotlib.pyplot as plt
        nx.draw(self.transforms, with_labels=True)
        plt.show()

    def to_svg(self):
        """
        """
        from ..graph import graph_to_svg
        return graph_to_svg(self.transforms)

    def __contains__(self, key):
        return key in self.transforms.node

    def __getitem__(self, key):
        return self.get(key)

    def __setitem__(self, key, value):
        value = np.asanyarray(value)
        if value.shape != (4, 4):
            raise ValueError('Matrix must be specified!')
        return self.update(key, matrix=value)

    def clear(self):
        self.transforms = EnforcedForest()
        self._paths = {}
        self._updated = str(np.random.random())
        self._cache.clear()

    def _get_path(self, frame_from, frame_to):
        """
        Find a path between two frames, either from cached paths or
        from the transform graph.

        Parameters
        ------------
        frame_from: a frame key, usually a string
                    eg, 'world'
        frame_to:   a frame key, usually a string
                    eg, 'mesh_0'

        Returns
        ----------
        path: (n) list of frame keys
              eg, ['mesh_finger', 'mesh_hand', 'world']
        """
        # store paths keyed as a tuple
        key = (frame_from, frame_to)
        if key not in self._paths:
            # get the actual shortest paths
            path = self.transforms.shortest_path_undirected(
                frame_from, frame_to)
            # store path to avoid recomputing
            self._paths[key] = path
            return path
        return self._paths[key]


class EnforcedForest(_ForestParent):
    """
    A subclass of networkx.DiGraph that will raise an error if an
    edge is added which would make the DiGraph not a forest or tree.
    """

    def __init__(self, *args, **kwargs):
        self.flags = {'strict': False, 'assert_forest': False}

        for k, v in self.flags.items():
            if k in kwargs:
                self.flags[k] = bool(kwargs[k])
                kwargs.pop(k, None)

        super(self.__class__, self).__init__(*args, **kwargs)
        # keep a second parallel but undirected copy of the graph
        # all of the networkx methods for turning a directed graph
        # into an undirected graph are quite slow so we do minor bookkeeping
        self._undirected = nx.Graph()

    def add_edge(self, u, v, *args, **kwargs):
        changed = False
        if u == v:
            if self.flags['strict']:
                raise ValueError('Edge must be between two unique nodes!')
            return changed
        if self._undirected.has_edge(u, v):
            self.remove_edges_from([[u, v], [v, u]])
        elif len(self.nodes()) > 0:
            try:
                path = nx.shortest_path(self._undirected, u, v)
                if self.flags['strict']:
                    raise ValueError(
                        'Multiple edge path exists between nodes!')
                self.disconnect_path(path)
                changed = True
            except (nx.NetworkXError, nx.NetworkXNoPath, nx.NetworkXException):
                pass
        self._undirected.add_edge(u, v)
        super(self.__class__, self).add_edge(u, v, *args, **kwargs)

        if self.flags['assert_forest']:
            # this is quite slow but makes very sure structure is correct
            # so is mainly used for testing
            assert nx.is_forest(nx.Graph(self))

        return changed

    def add_edges_from(self, *args, **kwargs):
        raise ValueError('EnforcedTree requires add_edge method to be used!')

    def add_path(self, *args, **kwargs):
        raise ValueError('EnforcedTree requires add_edge method to be used!')

    def remove_edge(self, *args, **kwargs):
        super(self.__class__, self).remove_edge(*args, **kwargs)
        self._undirected.remove_edge(*args, **kwargs)

    def remove_edges_from(self, *args, **kwargs):
        super(self.__class__, self).remove_edges_from(*args, **kwargs)
        self._undirected.remove_edges_from(*args, **kwargs)

    def disconnect_path(self, path):
        ebunch = np.array([[path[0], path[1]]])
        ebunch = np.vstack((ebunch, np.fliplr(ebunch)))
        self.remove_edges_from(ebunch)

    def shortest_path_undirected(self, u, v):
        try:
            path = nx.shortest_path(self._undirected, u, v)
        except BaseException as E:
            print(u, v)
            raise E
        return path

    def get_edge_data_direction(self, u, v):
        if self.has_edge(u, v):
            direction = 1
        elif self.has_edge(v, u):
            direction = -1
        else:
            raise ValueError('Edge does not exist!')
        data = self.get_edge_data(*[u, v][::direction])
        return data, direction


def kwargs_to_matrix(**kwargs):
    """
    Turn a set of keyword arguments into a transformation matrix.
    """
    if 'matrix' in kwargs:
        # a matrix takes precedence over other options
        matrix = np.asanyarray(kwargs['matrix'], dtype=np.float64)
    elif 'quaternion' in kwargs:
        matrix = transformations.quaternion_matrix(kwargs['quaternion'])
    elif ('axis' in kwargs) and ('angle' in kwargs):
        matrix = transformations.rotation_matrix(kwargs['angle'],
                                                 kwargs['axis'])
    else:
        raise ValueError('Couldn\'t update transform!')

    if 'translation' in kwargs:
        # translation can be used in conjunction with any of the methods of
        # specifying transforms. In the case a matrix and translation are passed,
        # we add the translations together rather than picking one.
        matrix[0:3, 3] += kwargs['translation']
    return matrix
