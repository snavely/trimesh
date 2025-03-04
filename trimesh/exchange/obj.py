import numpy as np
import collections

try:
    import PIL.Image as Image
except ImportError:
    pass

from .. import util

from ..visual.color import to_float
from ..visual.texture import unmerge_faces, TextureVisuals
from ..visual.material import SimpleMaterial

from ..constants import log, tol


def load_obj(file_obj,
             resolver=None,
             split_object=False,
             group_material=True,
             **kwargs):
    """
    Load a Wavefront OBJ file into kwargs for a trimesh.Scene
    object.

    Parameters
    --------------
    file_obj : file like object
      Contains OBJ data
    resolver : trimesh.visual.resolvers.Resolver
      Allow assets such as referenced textures and
      material files to be loaded
    split_object : bool
      Split meshes at each `o` declared in file
    group_material : bool
      Group faces that share the same material
      into the same mesh.

    Returns
    -------------
    kwargs : dict
      Keyword arguments which can be loaded by
      trimesh.exchange.load.load_kwargs into a trimesh.Scene
    """

    # get text as bytes or string blob
    text = file_obj.read()

    # if text was bytes decode into string
    text = util.decode_text(text)

    # add leading and trailing newlines so we can use the
    # same logic even if they jump directly in to data lines
    text = '\n{}\n'.format(text.strip().replace('\r\n', '\n'))

    # Load Materials
    materials = {}
    mtl_position = text.find('mtllib')
    if mtl_position >= 0:
        # take the line of the material file after `mtllib`
        # which should be the file location of the .mtl file
        mtl_path = text[mtl_position + 6:text.find('\n', mtl_position)].strip()
        try:
            # use the resolver to get the data
            material_kwargs = parse_mtl(resolver[mtl_path],
                                        resolver=resolver)
            # turn parsed kwargs into material objects
            materials = {k: SimpleMaterial(**v)
                         for k, v in material_kwargs.items()}
        except IOError:
            # usually the resolver couldn't find the asset
            log.warning('unable to load materials from: {}'.format(mtl_path))
        except BaseException:
            # something else happened so log a warning
            log.warning('unable to load materials from: {}'.format(mtl_path),
                        exc_info=True)

    # extract vertices from raw text
    v, vn, vt, vc = _parse_vertices(text=text)

    # get relevant chunks that have face data
    # in the form of (material, object, chunk)
    face_tuples = _preprocess_faces(
        text=text, split_object=split_object)

    # combine chunks that have the same material
    # some meshes end up with a LOT of components
    # and will be much slower if you don't do this
    if group_material:
        face_tuples = _group_by_material(face_tuples)

    # Load Faces
    # now we have clean- ish faces grouped by material and object
    # so now we have to turn them into numpy arrays and kwargs
    # for trimesh mesh and scene objects
    geometry = {}
    while len(face_tuples) > 0:
        # consume the next chunk of text
        material, current_object, chunk = face_tuples.pop()
        # do wangling in string form
        # we need to only take the face line before a newline
        # using builtin functions in a list comprehension
        # is pretty fast relative to other options
        # this operation is the only one that is O(len(faces))
        # slower due to the tight-loop conditional:
        # face_lines = [i[:i.find('\n')]
        #              for i in chunk.split('\nf ')[1:]
        #              if i.rfind('\n') >0]
        # maxsplit=1 means that it can stop working
        # after it finds the first newline
        # passed as arg as it's not a kwarg in python2
        face_lines = [i.split('\n', 1)[0]
                      for i in chunk.split('\nf ')[1:]]
        # then we are going to replace all slashes with spaces
        joined = ' '.join(face_lines).replace('/', ' ')

        # the fastest way to get to a numpy array
        # processes the whole string at once into a 1D array
        # also wavefront is 1-indexed (vs 0-indexed) so offset
        array = np.fromstring(joined, sep=' ', dtype=np.int64) - 1

        # get the number of raw 2D columns in a sample line
        columns = len(face_lines[0].strip().replace('/', ' ').split())

        # make sure we have the right number of values for vectorized
        if len(array) == (columns * len(face_lines)):
            # everything is a nice 2D array
            faces, faces_tex, faces_norm = _parse_faces_vectorized(
                array=array,
                columns=columns,
                sample_line=face_lines[0])
        else:
            # if we had something annoying like mixed in quads
            # or faces that differ per-line we have to loop
            # i.e. something like:
            #  '31407 31406 31408',
            #  '32303/2469 32304/2469 32305/2469',
            log.warning('faces have mixed data, using slow fallback!')
            faces, faces_tex, faces_norm = _parse_faces_fallback(face_lines)

        # TODO: name usually falls back to something useless
        name = current_object
        if name is None or len(name) == 0 or name in geometry:
            name = '{}_{}'.format(name, util.unique_id())

        # try to get usable texture
        mesh = kwargs.copy()
        if faces_tex is not None:
            # convert faces referencing vertices and
            # faces referencing vertex texture to new faces
            # where each face
            if faces_norm is not None and len(faces_norm) == len(faces):
                new_faces, mask_v, mask_vt, mask_vn = unmerge_faces(
                    faces, faces_tex, faces_norm)
            else:
                mask_vn = None
                new_faces, mask_v, mask_vt = unmerge_faces(faces, faces_tex)

            if tol.strict:
                # we should NOT have messed up the faces
                # note: this is EXTREMELY slow due to all the
                # float comparisons so only run this in unit tests
                assert np.allclose(v[faces], v[mask_v][new_faces])
                # faces should all be in bounds of vertives
                assert new_faces.max() < len(v[mask_v])

            try:
                # survive index errors as sometimes we
                # want materials without UV coordinates
                uv = vt[mask_vt]
            except BaseException as E:
                uv = None
                raise E

            # mask vertices and use new faces
            mesh.update({'vertices': v[mask_v].copy(),
                         'faces': new_faces})

        else:
            # otherwise just use unmasked vertices
            uv = None

            # check to make sure indexes are in bounds
            if tol.strict:
                assert faces.max() < len(v)

            if vn is not None and np.shape(faces_norm) == faces.shape:
                # do the crazy unmerging logic for split indices
                new_faces, mask_v, mask_vn = unmerge_faces(
                    faces, faces_norm)
            else:
                # generate the mask so we only include
                # referenced vertices in every new mesh
                mask_v = np.zeros(len(v), dtype=np.bool)
                mask_v[faces] = True

                # reconstruct the faces with the new vertex indices
                inverse = np.zeros(len(v), dtype=np.int64)
                inverse[mask_v] = np.arange(mask_v.sum())
                new_faces = inverse[faces]
                # no normals
                mask_vn = None

            # start with vertices and faces
            mesh.update({'faces': new_faces,
                         'vertices': v[mask_v].copy()})
            # if vertex colors are OK save them
            if vc is not None:
                mesh['vertex_colors'] = vc[mask_v]
            # if vertex normals are OK save them
            if mask_vn is not None:
                mesh['vertex_normals'] = vn[mask_vn]

        if materials is not None and material in materials:
            visual = TextureVisuals(
                uv=uv, material=materials[material])
        else:
            log.warning('specified material ({})  not loaded!'.format(
                material))
            visual = None
        mesh['visual'] = visual

        # store geometry by name
        geometry[name] = mesh

    if len(geometry) == 1:
        return next(iter(geometry.values()))

    # add an identity transform for every geometry
    graph = [{'geometry': k, 'frame_to': k, 'matrix': np.eye(4)}
             for k in geometry.keys()]

    # convert to scene kwargs
    result = {'geometry': geometry,
              'graph': graph}

    return result


def parse_mtl(mtl, resolver=None):
    """
    Parse a loaded MTL file.

    Parameters
    -------------
    mtl : str or bytes
      Data from an MTL file
    resolver : trimesh.visual.Resolver
      Fetch assets by name from files, web, or other

    Returns
    ------------
    mtllibs : list of dict
      Each dict has keys: newmtl, map_Kd, Kd
    """
    # decode bytes into string if necessary
    if hasattr(mtl, 'decode'):
        mtl = mtl.decode('utf-8')

    # current material
    material = None
    # materials referenced by name
    materials = {}
    # use universal newline splitting
    lines = str.splitlines(str(mtl).strip())

    for line in lines:
        # split by white space
        split = line.strip().split()
        # needs to be at least two values
        if len(split) <= 1:
            continue
        # the first value is the parameter name
        key = split[0]
        # start a new material
        if key == 'newmtl':
            # material name extracted from line like:
            # newmtl material_0
            if material is not None:
                # save the old material by old name and remove key
                materials[material.pop('newmtl')] = material
            # start a fresh new material
            material = {'newmtl': ' '.join(split[1:])}

        elif key == 'map_Kd':
            # represents the file name of the texture image
            try:
                file_data = resolver.get(split[1])
                # load the bytes into a PIL image
                # an image file name
                material['image'] = Image.open(
                    util.wrap_as_stream(file_data))
            except BaseException:
                log.warning('failed to load image', exc_info=True)

        elif key in ['Kd', 'Ka', 'Ks']:
            # remap to kwargs for SimpleMaterial
            mapped = {'Kd': 'diffuse',
                      'Ka': 'ambient',
                      'Ks': 'specular'}
            try:
                # diffuse, ambient, and specular float RGB
                material[mapped[key]] = [float(x) for x in split[1:]]
            except BaseException:
                log.warning('failed to convert color!', exc_info=True)

        elif material is not None:
            # save any other unspecified keys
            material[key] = split[1:]
    # reached EOF so save any existing materials
    if material:
        materials[material.pop('newmtl')] = material

    return materials


def _parse_faces_vectorized(array, columns, sample_line):
    """
    Parse loaded homogeneous (tri/quad) face data in a
    vectorized manner.

    Parameters
    ------------
    array : (n,) int
      Indices in order
    columns : int
      Number of columns in the file
    sample_line : str
      A single line so we can assess the ordering

    Returns
    --------------
    faces : (n, d) int
      Faces in space
    faces_tex : (n, d) int or None
      Texture for each vertex in face
    faces_norm : (n, d) int or None
      Normal index for each vertex in face
    """
    # reshape to columns
    array = array.reshape((-1, columns))
    # how many elements are in the first line of faces
    # i.e '13/1/13 14/1/14 2/1/2 1/2/1' is 4
    group_count = len(sample_line.strip().split())
    # how many elements are there for each vertex reference
    # i.e. '12/1/13' is 3
    per_ref = int(columns / group_count)
    # create an index mask we can use to slice vertex references
    index = np.arange(group_count) * per_ref
    # slice the faces out of the blob array
    faces = array[:, index]

    # TODO: probably need to support 8 and 12 columns for quads
    # or do something more general
    faces_tex, faces_norm = None, None
    if columns == 6:
        # if we have two values per vertex the second
        # one is index of texture coordinate (`vt`)
        # count how many delimiters are in the first face line
        # to see if our second value is texture or normals
        count = sample_line.count('/')
        if count == columns:
            # case where each face line looks like:
            # ' 75//139 76//141 77//141'
            # which is vertex/nothing/normal
            faces_norm = array[:, index + 1]
        elif count == int(columns / 2):
            # case where each face line looks like:
            # '75/139 76/141 77/141'
            # which is vertex/texture
            faces_tex = array[:, index + 1]
        else:
            log.warning('face lines are weird: {}'.format(
                sample_line))
    elif columns == 9:
        # if we have three values per vertex
        # second value is always texture
        faces_tex = array[:, index + 1]
        # third value is reference to vertex normal (`vn`)
        faces_norm = array[:, index + 2]
    return faces, faces_tex, faces_norm


def _parse_faces_fallback(lines):
    """
    Use a slow but more flexible looping method to process
    face lines as a fallback option to faster vectorized methods.

    Parameters
    -------------
    lines : (n,) str
      List of lines with face information

    Returns
    -------------
    faces : (m, 3) int
      Clean numpy array of face triangles
    """

    # collect vertex, texture, and vertex normal indexes
    v, vt, vn = [], [], []

    # loop through every line starting with a face
    for line in lines:
        # remove leading newlines then
        # take first bit before newline then split by whitespace
        split = line.strip().split('\n')[0].split()
        # split into: ['76/558/76', '498/265/498', '456/267/456']
        if len(split) == 4:
            # triangulate quad face
            split = [split[0],
                     split[1],
                     split[2],
                     split[2],
                     split[3],
                     split[0]]
        elif len(split) != 3:
            log.warning(
                'face has {} elements! skipping!'.format(len(split)))
            continue

        # f is like: '76/558/76'
        for f in split:
            # vertex, vertex texture, vertex normal
            split = f.split('/')
            # we always have a vertex reference
            v.append(int(split[0]))

            # faster to try/except than check in loop
            try:
                vt.append(int(split[1]))
            except BaseException:
                pass
            try:
                # vertex normal is the third index
                vn.append(int(split[2]))
            except BaseException:
                pass

    # shape into triangles and switch to 0-indexed
    faces = np.array(v, dtype=np.int64).reshape((-1, 3)) - 1
    faces_tex, normals = None, None
    if len(vt) == len(v):
        faces_tex = np.array(vt, dtype=np.int64).reshape((-1, 3)) - 1
    if len(vn) == len(v):
        normals = np.array(vn, dtype=np.int64).reshape((-1, 3)) - 1

    return faces, faces_tex, normals


def _parse_vertices(text):
    """
    Parse raw OBJ text into vertices, vertex normals,
    vertex colors, and vertex textures.

    Parameters
    -------------
    text : str
      Full text of an OBJ file

    Returns
    -------------
    v : (n, 3) float
      Vertices in space
    vn : (m, 3) float or None
      Vertex normals
    vt : (p, 2) float or None
      Vertex texture coordinates
    vc : (n, 3) float or None
      Per-vertex color
    """

    # the first position of a vertex in the text blob
    # we only really need to search from the start of the file
    # up to the location of out our first vertex but we
    # are going to use this check for "do we have texture"
    # determination later so search the whole stupid file
    starts = {k: text.find('\n{} '.format(k)) for k in
              ['v', 'vt', 'vn']}

    # no valid values so exit early
    if not any(v >= 0 for v in starts.values()):
        return None, None, None, None

    # find the last position of each valid value
    ends = {k: text.find(
        '\n', text.rfind('\n{} '.format(k)) + 2 + len(k))
        for k, v in starts.items() if v >= 0}

    # take the first and last position of any vertex property
    start = min(s for s in starts.values() if s >= 0)
    end = max(e for e in ends.values() if e >= 0)
    # get the chunk of test that contains vertex data
    chunk = text[start:end].replace('+e', 'e').replace('-e', 'e')

    # get the clean-ish data from the file as python lists
    data = {k: [i.split('\n', 1)[0]
                for i in chunk.split('\n{} '.format(k))[1:]]
            for k, v in starts.items() if v >= 0}

    # count the number of data values per row on a sample row
    per_row = {k: len(v[1].split()) for k, v in data.items()}

    # convert data values into numpy arrays
    result = collections.defaultdict(lambda: None)
    for k, value in data.items():
        # use joining and fromstring to get as numpy array
        array = np.fromstring(
            ' '.join(value), sep=' ', dtype=np.float64)
        # what should our shape be
        shape = (len(value), per_row[k])
        # check shape of flat data
        if len(array) == np.product(shape):
            # we have a nice 2D array
            result[k] = array.reshape(shape)
        else:
            # try to recover with a slightly more expensive loop
            count = per_row[k]
            try:
                # try to get result through reshaping
                result[k] = np.fromstring(
                    ' '.join(i.split()[:count] for i in value),
                    sep=' ', dtype=np.float64).reshape(shape)
            except BaseException:
                pass

    # vertices
    v = result['v']
    # vertex colors are stored next to vertices
    vc = None
    if v is not None and v.shape[1] >= 6:
        # vertex colors are stored after vertices
        v, vc = v[:, :3], v[:, 3:6]
    elif v is not None and v.shape[1] > 3:
        # we got a lot of something unknowable
        v = v[:, :3]

    # vertex texture or None
    vt = result['vt']
    if vt is not None:
        # sometimes UV coordinates come in as UVW
        vt = vt[:, :2]
    # vertex normals or None
    vn = result['vn']

    # check will generally only be run in unit tests
    # so we are allowed to do things that are slow
    if tol.strict:
        # check to make sure our subsetting
        # didn't miss any vertices or data
        assert len(v) == text.count('\nv ')
        # make sure optional data matches file too
        if vn is not None:
            assert len(vn) == text.count('\nvn ')
        if vt is not None:
            assert len(vt) == text.count('\nvt ')

    return v, vn, vt, vc


def _group_by_material(face_tuples):
    """
    For chunks of faces split by material group
    the chunks that share the same material.

    Parameters
    ------------
    face_tuples : (n,) list of (material, obj, chunk)
      The data containing faces

    Returns
    ------------
    grouped : (m,) list of (material, obj, chunk)
      Grouped by material
    """

    # store the chunks grouped by material
    grouped = collections.defaultdict(lambda: ['', '', []])
    # loop through existring
    for material, obj, chunk in face_tuples:
        grouped[material][0] = material
        grouped[material][1] = obj
        # don't do a million string concatenations in loop
        grouped[material][2].append(chunk)
    # go back and do a join to make a single string
    for k in grouped.keys():
        grouped[k][2] = '\n'.join(grouped[k][2])
    # return as list
    return list(grouped.values())


def _preprocess_faces(text, split_object=False):
    # Pre-Process Face Text
    # Rather than looking at each line in a loop we're
    # going to split lines by directives which indicate
    # a new mesh, specifically 'usemtl' and 'o' keys
    # search for materials, objects, faces, or groups
    starters = ['\nusemtl ', '\no ', '\nf ', '\ng ', '\ns ']
    f_start = len(text)
    # first index of material, object, face, group, or smoother
    for st in starters:
        search = text.find(st, 0, f_start)
        # if not contained find will return -1
        if search < 0:
            continue
        # subtract the length of the key from the position
        # to make sure it's included in the slice of text
        if search < f_start:
            f_start = search
    # index in blob of the newline after the last face
    f_end = text.find('\n', text.rfind('\nf ') + 3)
    # get the chunk of the file that has face information
    if f_end >= 0:
        # clip to the newline after the last face
        f_chunk = text[f_start:f_end]
    else:
        # no newline after last face
        f_chunk = text[f_start:]

    if tol.strict:
        # check to make sure our subsetting didn't miss any faces
        assert f_chunk.count('\nf ') == text.count('\nf ')

    # start with undefined objects and material
    current_object = None
    current_material = None
    # where we're going to store result tuples
    # containing (material, object, face lines)
    face_tuples = []

    # two things cause new meshes to be created: objects and materials
    # first divide faces into groups split by material and objects
    # face chunks using different materials will be treated
    # as different meshes
    for m_chunk in f_chunk.split('\nusemtl '):
        # if empty continue
        if len(m_chunk) == 0:
            continue
        # find the first newline in the chunk
        # everything before it will be the usemtl direction
        newline = m_chunk.find('\n')
        # remove internal double spaces because why wouldn't that be OK
        current_material = ' '.join(m_chunk[:newline].strip().split())
        # material chunk contains multiple objects
        if split_object:
            o_split = m_chunk.split('\no ')
        else:
            o_split = [m_chunk]
        if len(o_split) > 1:
            for o_chunk in o_split:
                # set the object label
                current_object = o_chunk[:o_chunk.find('\n')].strip()
                # find the first face in the chunk
                f_idx = o_chunk.find('\nf ')
                # if we have any faces append it to our search tuple
                if f_idx >= 0:
                    face_tuples.append(
                        (current_material,
                         current_object,
                         o_chunk[f_idx:]))
        else:
            # if there are any faces in this chunk add them
            f_idx = m_chunk.find('\nf ')
            if f_idx >= 0:
                face_tuples.append(
                    (current_material,
                     current_object,
                     m_chunk[f_idx:]))
    return face_tuples


def export_obj(mesh,
               include_normals=True,
               include_color=True):
    """
    Export a mesh as a Wavefront OBJ file

    Parameters
    -----------
    mesh : trimesh.Trimesh
      Mesh to be exported

    Returns
    -----------
    export : str
      OBJ format output
    """
    # store the multiple options for formatting
    # vertex indexes for faces
    face_formats = {('v',): '{}',
                    ('v', 'vn'): '{}//{}',
                    ('v', 'vt'): '{}/{}',
                    ('v', 'vn', 'vt'): '{}/{}/{}'}
    # we are going to reference face_formats with this
    face_type = ['v']

    # OBJ includes vertex color as RGB elements on the same line
    if include_color and mesh.visual.kind in ['vertex', 'face']:
        # create a stacked blob with position and color
        v_blob = np.column_stack((
            mesh.vertices,
            to_float(mesh.visual.vertex_colors[:, :3])))
    else:
        # otherwise just export vertices
        v_blob = mesh.vertices

    # add the first vertex key and convert the array
    export = 'v ' + util.array_to_string(v_blob,
                                         col_delim=' ',
                                         row_delim='\nv ',
                                         digits=8) + '\n'

    # only include vertex normals if they're already stored
    if include_normals and 'vertex_normals' in mesh._cache:
        # if vertex normals are stored in cache export them
        face_type.append('vn')
        export += 'vn '
        export += util.array_to_string(mesh.vertex_normals,
                                       col_delim=' ',
                                       row_delim='\nvn ',
                                       digits=8) + '\n'

    """
    TODO: update this to use TextureVisuals
    if include_texture:
        # if vertex texture exists and is the right shape export here
        face_type.append('vt')
        export += 'vt '

        export += util.array_to_string(mesh.metadata['vertex_texture'],
                                       col_delim=' ',
                                       row_delim='\nvt ',
                                       digits=8) + '\n'
    """

    # the format for a single vertex reference of a face
    face_format = face_formats[tuple(face_type)]
    faces = 'f ' + util.array_to_string(mesh.faces + 1,
                                        col_delim=' ',
                                        row_delim='\nf ',
                                        value_format=face_format)
    # add the exported faces to the export
    export += faces

    return export


_obj_loaders = {'obj': load_obj}
_obj_exporters = {'obj': export_obj}
