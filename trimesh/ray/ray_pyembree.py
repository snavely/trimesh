"""
Ray queries using the pyembree package with the
API wrapped to match our native raytracer.
"""
import numpy as np

from collections import deque
from copy import deepcopy

from pyembree import __version__ as _ver
from pyembree import rtcore_scene
from pyembree.mesh_construction import TriangleMesh

from pkg_resources import parse_version

from .ray_util import contains_points

from .. import util
from .. import caching
from .. import intersections

# the factor of geometry.scale to offset a ray from a triangle
# to reliably not hit its origin triangle
_ray_offset_factor = 1e-4
# we want to clip our offset to a sane distance
_ray_offset_floor = 1e-8

# see if we're using a newer version of the pyembree wrapper
_embree_new = parse_version(_ver) >= parse_version('0.1.4')
# both old and new versions require exact but different type
_embree_dtype = [np.float64, np.float32][int(_embree_new)]


class RayMeshIntersector(object):

    def __init__(self,
                 geometry,
                 scale_to_box=True):
        """
        Do ray- mesh queries.

        Parameters
        -------------
        geometry     : Trimesh object
                         Mesh to do ray tests on
        scale_to_box : bool
                         If true, will scale mesh to approximate
                         unit cube to avoid problems with extreme
                         large or small meshes.
        """
        self.mesh = geometry
        self._scale_to_box = scale_to_box
        self._cache = caching.Cache(id_function=self.mesh.crc)

    @property
    def _scale(self):
        """
        Scaling factor for precision.
        """
        if self._scale_to_box:
            # scale vertices to approximately a cube to help with
            # numerical issues at very large/small scales
            scale = 100.0 / self.mesh.scale
        else:
            scale = 1.0
        return scale

    @caching.cache_decorator
    def _scene(self):
        """
        A cached version of the pyembree scene.
        """
        return _EmbreeWrap(vertices=self.mesh.vertices,
                           faces=self.mesh.faces,
                           scale=self._scale)

    def intersects_location(self,
                            ray_origins,
                            ray_directions,
                            multiple_hits=True):
        """
        Return the location of where a ray hits a surface.

        Parameters
        ----------
        ray_origins:    (n,3) float, origins of rays
        ray_directions: (n,3) float, direction (vector) of rays


        Returns
        ---------
        locations: (n) sequence of (m,3) intersection points
        index_ray: (n,) int, list of ray index
        index_tri: (n,) int, list of triangle (face) indexes
        """
        (index_tri,
         index_ray,
         locations) = self.intersects_id(
             ray_origins=ray_origins,
             ray_directions=ray_directions,
             multiple_hits=multiple_hits,
             return_locations=True)

        return locations, index_ray, index_tri

    def intersects_id(self,
                      ray_origins,
                      ray_directions,
                      multiple_hits=True,
                      max_hits=20,
                      return_locations=False):
        """
        Find the triangles hit by a list of rays, including
        optionally multiple hits along a single ray.

        Parameters
        ----------
        ray_origins:      (n,3) float, origins of rays
        ray_directions:   (n,3) float, direction (vector) of rays
        multiple_hits:    bool, if True will return every hit along the ray
                                if False will only return first hit
        return_locations: bool, should we return hit locations or not

        Returns
        ----------
        index_tri: (m,) int, index of triangle the ray hit
        index_ray: (m,) int, index of ray
        locations: (m,3) float, locations in space
        """
        # make sure input is _dtype for embree
        ray_origins = np.asanyarray(
            deepcopy(ray_origins),
            dtype=np.float64)
        ray_directions = np.asanyarray(ray_directions,
                                       dtype=np.float64)
        ray_directions = util.unitize(ray_directions)

        # since we are constructing all hits, save them to a deque then
        # stack into (depth, len(rays)) at the end
        result_triangle = deque()
        result_ray_idx = deque()
        result_locations = deque()

        # the mask for which rays are still active
        current = np.ones(len(ray_origins), dtype=np.bool)

        if multiple_hits or return_locations:
            # how much to offset ray to transport to the other side of face
            distance = np.clip(_ray_offset_factor * self._scale,
                               _ray_offset_floor,
                               np.inf)
            ray_offsets = ray_directions * distance

            # grab the planes from triangles
            plane_origins = self.mesh.triangles[:, 0, :]
            plane_normals = self.mesh.face_normals

        # use a for loop rather than a while to ensure this exits
        # if a ray is offset from a triangle and then is reported
        # hitting itself this could get stuck on that one triangle
        for query_depth in range(max_hits):
            # run the pyembree query
            # if you set output=1 it will calculate distance along
            # ray, which is bizzarely slower than our calculation
            query = self._scene.run(
                ray_origins[current],
                ray_directions[current])

            # basically we need to reduce the rays to the ones that hit
            # something
            hit = query != -1
            # which triangle indexes were hit
            hit_triangle = query[hit]

            # eliminate rays that didn't hit anything from future queries
            current_index = np.nonzero(current)[0]
            current_index_no_hit = current_index[np.logical_not(hit)]
            current_index_hit = current_index[hit]
            current[current_index_no_hit] = False

            # append the triangle and ray index to the results
            result_triangle.append(hit_triangle)
            result_ray_idx.append(current_index_hit)

            # if we don't need all of the hits, return the first one
            if ((not multiple_hits and
                 not return_locations) or
                    not hit.any()):
                break

            # find the location of where the ray hit the triangle plane
            new_origins, valid = intersections.planes_lines(
                plane_origins=plane_origins[hit_triangle],
                plane_normals=plane_normals[hit_triangle],
                line_origins=ray_origins[current],
                line_directions=ray_directions[current])

            if not valid.all():
                # since a plane intersection was invalid we have to go back and
                # fix some stuff, we pop the ray index and triangle index,
                # apply the valid mask then append it right back to keep our
                # indexes intact
                result_ray_idx.append(result_ray_idx.pop()[valid])
                result_triangle.append(result_triangle.pop()[valid])

                # update the current rays to reflect that we couldn't find a
                # new origin
                current[current_index_hit[np.logical_not(valid)]] = False

            # since we had to find the intersection point anyway we save it
            # even if we're not going to return it
            result_locations.extend(new_origins)

            if multiple_hits:
                # move the ray origin to the other side of the triangle
                ray_origins[current] = new_origins + ray_offsets[current]
            else:
                break

        # stack the deques into nice 1D numpy arrays
        index_tri = np.hstack(result_triangle)
        index_ray = np.hstack(result_ray_idx)

        if return_locations:
            locations = (
                np.zeros((0, 3), float) if len(result_locations) == 0
                else np.array(result_locations))

            return index_tri, index_ray, locations
        return index_tri, index_ray

    def intersects_first(self,
                         ray_origins,
                         ray_directions):
        """
        Find the index of the first triangle a ray hits.


        Parameters
        ----------
        ray_origins:    (n,3) float, origins of rays
        ray_directions: (n,3) float, direction (vector) of rays

        Returns
        ----------
        triangle_index: (n,) int, index of triangle ray hit, or -1 if not hit
        """

        ray_origins = np.asanyarray(deepcopy(ray_origins))
        ray_directions = np.asanyarray(ray_directions)

        triangle_index = self._scene.run(ray_origins,
                                         ray_directions)
        return triangle_index

    def intersects_any(self,
                       ray_origins,
                       ray_directions):
        """
        Check if a list of rays hits the surface.


        Parameters
        ----------
        ray_origins:    (n,3) float, origins of rays
        ray_directions: (n,3) float, direction (vector) of rays

        Returns
        ----------
        hit:            (n,) bool, did each ray hit the surface
        """

        first = self.intersects_first(ray_origins=ray_origins,
                                      ray_directions=ray_directions)
        hit = first != -1
        return hit

    def contains_points(self, points):
        """
        Check if a mesh contains a list of points, using ray tests.

        If the point is on the surface of the mesh, behavior is undefined.

        Parameters
        ---------
        points: (n,3) points in space

        Returns
        ---------
        contains: (n,) bool
                         Whether point is inside mesh or not
        """
        return contains_points(self, points)


class _EmbreeWrap(object):
    """
    A light wrapper for PyEmbree scene objects which
    allows queries to be scaled to help with precision
    issues, as well as selecting the correct dtypes.
    """

    def __init__(self, vertices, faces, scale):
        scaled = np.asanyarray(vertices,
                               dtype=np.float64)
        self.origin = scaled.min(axis=0)
        self.scale = float(scale)
        scaled = (scaled - self.origin) * self.scale

        self.scene = rtcore_scene.EmbreeScene()
        # assign the geometry to the scene
        TriangleMesh(
            scene=self.scene,
            vertices=scaled.astype(_embree_dtype),
            indices=faces.astype(np.int32))

    def run(self, origins, normals, **kwargs):
        scaled = (np.asanyarray(origins,
                                dtype=np.float64) - self.origin) * self.scale

        return self.scene.run(scaled.astype(_embree_dtype),
                              normals.astype(_embree_dtype),
                              **kwargs)
