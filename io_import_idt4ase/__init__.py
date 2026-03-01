## ***** BEGIN GPL LICENSE BLOCK *****
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
#
# ***** END GPL LICENCE BLOCK *****

bl_info = {
    "name": "ASE Importer for idTech 4",
    "author": "motorsep/Claude",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "File > Import > ASCII Scene Import (.ase)",
    "description": "Import static meshes from ASCII Scene Export (.ase) for idTech 4",
    "warning": "",
    "wiki_url": "",
    "tracker_url": "",
    "category": "Import-Export",
}

"""
ASE Importer for idTech 4 (Doom 3, Quake 4, Prey, Dark Mod, StormEngine2, etc.)

Imports .ase files into Blender, creating mesh objects with:
- Materials (from MATERIAL_LIST, assigned per GEOMOBJECT via MATERIAL_REF)
- UV coordinates (from MESH_TVERT / MESH_TFACE, handles both shared and
  sequential UV index styles)
- Vertex colors (from MESH_VERTCOL / MESH_CFACE)
- Custom split normals (from MESH_NORMALS per-face vertex normals)
- Sharp edges (derived from MESH_SMOOTHING group boundaries)

Handles both 3DS Max native exports and Blender ASE exporter output.
"""

import os
import re
import bpy
import bmesh
import time
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty, BoolProperty, FloatProperty


# =============================================================================
# Parsed ASE data structures
# =============================================================================

class ASEMaterial:
    """Parsed material from MATERIAL_LIST."""
    __slots__ = ('name', 'diffuse', 'specular', 'bitmap',
                 'u_offset', 'v_offset', 'u_tiling', 'v_tiling', 'angle')

    def __init__(self):
        self.name = ''
        self.diffuse = (0.8, 0.8, 0.8)
        self.specular = (1.0, 1.0, 1.0)
        self.bitmap = ''
        self.u_offset = 0.0
        self.v_offset = 0.0
        self.u_tiling = 1.0
        self.v_tiling = 1.0
        self.angle = 0.0


class ASEGeomObject:
    """Parsed geometry from a single GEOMOBJECT block."""
    __slots__ = ('name', 'material_ref',
                 'vertices', 'faces', 'smoothing_groups',
                 'tvertices', 'tfaces',
                 'cvertices', 'cfaces',
                 'vertex_normals')

    def __init__(self):
        self.name = ''
        self.material_ref = 0
        self.vertices = []          # list of (x, y, z)
        self.faces = []             # list of (v0, v1, v2)
        self.smoothing_groups = []  # list of int, per face
        self.tvertices = []         # list of (u, v)
        self.tfaces = []            # list of (t0, t1, t2)
        self.cvertices = []         # list of (r, g, b)
        self.cfaces = []            # list of (c0, c1, c2)
        # Per-face vertex normals: vertex_normals[face_idx] = [(nx,ny,nz) * 3]
        # Stored in the order the MESH_VERTEXNORMAL lines appear under each
        # MESH_FACENORMAL (matches the A, B, C order of the face).
        self.vertex_normals = []


# =============================================================================
# ASE Tokenizing Parser
# =============================================================================

class ASEParser:
    """Tokenizing parser for ASE files.

    Reads the file into memory and walks through tokens, handling braced
    blocks just like the idTech 4 engine's ASE_ParseBracedBlock / ASE_GetToken.
    """

    def __init__(self, text):
        self.materials = []
        self.objects = []
        self._buf = text
        self._pos = 0
        self._len = len(text)

    # -- Tokenizer -----------------------------------------------------------

    def _get_token(self, rest_of_line=False):
        """Return the next whitespace-delimited token, or None at EOF.

        If *rest_of_line* is True, return everything up to the next newline.
        Quoted strings are returned without the quotes.
        """
        buf = self._buf
        pos = self._pos
        length = self._len

        # Skip whitespace
        while pos < length and buf[pos] <= ' ':
            pos += 1
        if pos >= length:
            self._pos = pos
            return None

        if rest_of_line:
            start = pos
            while pos < length and buf[pos] not in '\r\n':
                pos += 1
            self._pos = pos
            return buf[start:pos].strip()

        ch = buf[pos]

        # Quoted string
        if ch == '"':
            pos += 1
            start = pos
            while pos < length and buf[pos] != '"':
                pos += 1
            token = buf[start:pos]
            if pos < length:
                pos += 1  # skip closing quote
            self._pos = pos
            return token

        # Normal token
        start = pos
        while pos < length and buf[pos] > ' ':
            pos += 1
        self._pos = pos
        return buf[start:pos]

    def _skip_block(self):
        """Skip a { ... } block, including nested blocks."""
        depth = 0
        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                depth -= 1
                if depth <= 0:
                    return

    def _get_float(self):
        return float(self._get_token())

    def _get_int(self):
        return int(self._get_token())

    # -- Top-level -----------------------------------------------------------

    def parse(self):
        """Parse the entire ASE file."""
        while True:
            t = self._get_token()
            if t is None:
                break
            if t == '*MATERIAL_LIST':
                self._parse_material_list()
            elif t == '*GEOMOBJECT':
                self._parse_geomobject()
            elif t == '*GROUP':
                self._get_token()  # group name
                self._parse_group()
            # Everything else (*SCENE, *COMMENT, *SHAPEOBJECT, etc.) is
            # either a rest-of-line keyword or a braced block we skip.

    # -- GROUP ---------------------------------------------------------------

    def _parse_group(self):
        """Parse a *GROUP block that may contain nested GEOMOBJECTs."""
        depth = 0
        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                depth -= 1
                if depth <= 0:
                    return
            elif t == '*GEOMOBJECT':
                self._parse_geomobject()

    # -- MATERIAL_LIST -------------------------------------------------------

    def _parse_material_list(self):
        depth = 0
        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                depth -= 1
                if depth <= 0:
                    return
            elif t == '*MATERIAL_COUNT':
                self._get_token()  # count (informational)
            elif t == '*MATERIAL':
                self._get_token()  # index
                self._parse_material()

    def _parse_material(self):
        mat = ASEMaterial()
        depth = 0
        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                depth -= 1
                if depth <= 0:
                    break
            elif t == '*MATERIAL_NAME':
                mat.name = self._get_token()
            elif t == '*MATERIAL_DIFFUSE':
                mat.diffuse = (self._get_float(), self._get_float(),
                               self._get_float())
            elif t == '*MATERIAL_SPECULAR':
                mat.specular = (self._get_float(), self._get_float(),
                                self._get_float())
            elif t == '*MAP_DIFFUSE':
                self._parse_map_diffuse(mat)
        self.materials.append(mat)

    def _parse_map_diffuse(self, mat):
        depth = 0
        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                depth -= 1
                if depth <= 0:
                    return
            elif t == '*BITMAP':
                mat.bitmap = self._get_token()
            elif t == '*UVW_U_OFFSET':
                mat.u_offset = self._get_float()
            elif t == '*UVW_V_OFFSET':
                mat.v_offset = self._get_float()
            elif t == '*UVW_U_TILING':
                mat.u_tiling = self._get_float()
            elif t == '*UVW_V_TILING':
                mat.v_tiling = self._get_float()
            elif t == '*UVW_ANGLE':
                mat.angle = self._get_float()

    # -- GEOMOBJECT ----------------------------------------------------------

    def _parse_geomobject(self):
        obj = ASEGeomObject()
        depth = 0
        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                depth -= 1
                if depth <= 0:
                    break
            elif t == '*NODE_NAME':
                obj.name = self._get_token()
            elif t == '*MATERIAL_REF':
                obj.material_ref = self._get_int()
            elif t == '*MESH':
                self._parse_mesh(obj)
            elif t in ('*NODE_TM', '*TM_ANIMATION', '*MESH_ANIMATION'):
                self._skip_block()

        self.objects.append(obj)

    # -- MESH ----------------------------------------------------------------

    def _parse_mesh(self, obj):
        depth = 0
        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                depth -= 1
                if depth <= 0:
                    return
            elif t == '*MESH_VERTEX_LIST':
                self._parse_vertex_list(obj)
            elif t == '*MESH_FACE_LIST':
                self._parse_face_list(obj)
            elif t == '*MESH_TVERTLIST':
                self._parse_tvert_list(obj)
            elif t == '*MESH_TFACELIST':
                self._parse_tface_list(obj)
            elif t == '*MESH_CVERTLIST':
                self._parse_cvert_list(obj)
            elif t == '*MESH_CFACELIST':
                self._parse_cface_list(obj)
            elif t == '*MESH_NORMALS':
                self._parse_normals(obj)
            elif t == '*MESH_MAPPINGCHANNEL':
                # Additional UV channels - skip for now
                self._skip_block()
            elif t in ('*MESH_NUMVERTEX', '*MESH_NUMFACES',
                       '*MESH_NUMTVERTEX', '*MESH_NUMTVFACES',
                       '*MESH_NUMCVERTEX', '*MESH_NUMCVFACES',
                       '*TIMEVALUE'):
                self._get_token()  # consume the count

    # -- Vertex list ---------------------------------------------------------

    def _parse_vertex_list(self, obj):
        depth = 0
        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                depth -= 1
                if depth <= 0:
                    return
            elif t == '*MESH_VERTEX':
                self._get_token()  # index
                obj.vertices.append(
                    (self._get_float(), self._get_float(), self._get_float()))

    # -- Face list -----------------------------------------------------------

    def _parse_face_list(self, obj):
        depth = 0
        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                depth -= 1
                if depth <= 0:
                    return
            elif t == '*MESH_FACE':
                self._parse_single_face(obj)

    def _parse_single_face(self, obj):
        """Parse one MESH_FACE line.

        Format:
            *MESH_FACE N:  A: v0 B: v1 C: v2  AB: e0 BC: e1 CA: e2
                *MESH_SMOOTHING <sg>  *MESH_MTLID <id>
        """
        self._get_token()  # face number (e.g. "0:")

        # A: v0  B: v1  C: v2
        self._get_token()  # "A:"
        v0 = self._get_int()
        self._get_token()  # "B:"
        v1 = self._get_int()
        self._get_token()  # "C:"
        v2 = self._get_int()
        obj.faces.append((v0, v1, v2))

        # Rest of line: edge flags, smoothing group, mtlid
        rest = self._get_token(rest_of_line=True)
        sg = 0
        if rest:
            m = re.search(r'\*MESH_SMOOTHING\s+(\d+)', rest)
            if m:
                sg = int(m.group(1))
        obj.smoothing_groups.append(sg)

    # -- Texture vertex / face lists -----------------------------------------

    def _parse_tvert_list(self, obj):
        depth = 0
        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                depth -= 1
                if depth <= 0:
                    return
            elif t == '*MESH_TVERT':
                self._get_token()  # index
                u = self._get_float()
                v = self._get_float()
                self._get_token()  # w (discard)
                obj.tvertices.append((u, v))

    def _parse_tface_list(self, obj):
        depth = 0
        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                depth -= 1
                if depth <= 0:
                    return
            elif t == '*MESH_TFACE':
                self._get_token()  # face index
                obj.tfaces.append(
                    (self._get_int(), self._get_int(), self._get_int()))

    # -- Color vertex / face lists -------------------------------------------

    def _parse_cvert_list(self, obj):
        depth = 0
        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                depth -= 1
                if depth <= 0:
                    return
            elif t == '*MESH_VERTCOL':
                self._get_token()  # index
                obj.cvertices.append(
                    (self._get_float(), self._get_float(), self._get_float()))

    def _parse_cface_list(self, obj):
        depth = 0
        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                depth -= 1
                if depth <= 0:
                    return
            elif t == '*MESH_CFACE':
                self._get_token()  # face index
                obj.cfaces.append(
                    (self._get_int(), self._get_int(), self._get_int()))

    # -- Normals -------------------------------------------------------------

    def _parse_normals(self, obj):
        """Parse MESH_NORMALS block.

        Structure per face:
            *MESH_FACENORMAL <face_idx>  <nx> <ny> <nz>
                *MESH_VERTEXNORMAL <vert_idx>  <nx> <ny> <nz>   (x3)

        We store three vertex normals per face in the order they appear,
        which matches the face's A, B, C vertex order as written in the file.
        """
        depth = 0
        current_vnorms = []

        while True:
            t = self._get_token()
            if t is None:
                return
            if t == '{':
                depth += 1
            elif t == '}':
                if current_vnorms:
                    obj.vertex_normals.append(current_vnorms)
                    current_vnorms = []
                depth -= 1
                if depth <= 0:
                    return
            elif t == '*MESH_FACENORMAL':
                # Flush previous face's normals
                if current_vnorms:
                    obj.vertex_normals.append(current_vnorms)
                current_vnorms = []
                self._get_token()  # face index
                # Skip the face normal xyz (we use per-vertex normals)
                self._get_float()
                self._get_float()
                self._get_float()
            elif t == '*MESH_VERTEXNORMAL':
                self._get_token()  # vertex index (skip, we use order)
                nx = self._get_float()
                ny = self._get_float()
                nz = self._get_float()
                current_vnorms.append((nx, ny, nz))


# =============================================================================
# Blender mesh builder
# =============================================================================

class ASEMeshBuilder:
    """Creates Blender mesh objects from parsed ASE data."""

    def __init__(self, parser, options):
        self.parser = parser
        self.options = options
        self.bl_materials = []  # Blender materials, indexed by ASE material #

    def build(self):
        """Create all Blender objects. Returns list of created objects."""
        self._create_materials()

        created = []
        for geom in self.parser.objects:
            obj = self._create_object(geom)
            if obj is not None:
                created.append(obj)
        return created

    # -- Materials -----------------------------------------------------------

    def _create_materials(self):
        """Create Blender materials from parsed ASE materials."""
        for ase_mat in self.parser.materials:
            mat = bpy.data.materials.new(name=ase_mat.name)
            mat.use_nodes = True
            tree = mat.node_tree
            nodes = tree.nodes
            links = tree.links
            nodes.clear()

            output = nodes.new('ShaderNodeOutputMaterial')
            output.location = (300, 0)

            bsdf = nodes.new('ShaderNodeBsdfPrincipled')
            bsdf.location = (0, 0)
            bsdf.inputs['Base Color'].default_value = (*ase_mat.diffuse, 1.0)
            links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

            self.bl_materials.append(mat)

    # -- Object creation -----------------------------------------------------

    def _create_object(self, geom):
        """Create one Blender mesh object from an ASEGeomObject."""
        if not geom.vertices or not geom.faces:
            print(f'  Skipping "{geom.name}" (empty geometry)')
            return None

        scale = self.options.get('scale', 1.0)
        num_faces = len(geom.faces)

        # Apply scale to vertices
        verts = [(x * scale, y * scale, z * scale)
                 for x, y, z in geom.vertices]

        # Create mesh data
        mesh = bpy.data.meshes.new(geom.name)
        mesh.from_pydata(verts, [], list(geom.faces))

        # Assign material
        if self.bl_materials:
            idx = geom.material_ref
            if idx < len(self.bl_materials):
                mesh.materials.append(self.bl_materials[idx])
            else:
                mesh.materials.append(self.bl_materials[0])
                print(f'  Warning: "{geom.name}" MATERIAL_REF {idx} '
                      f'out of range, using material 0')

        # UVs
        if geom.tvertices and geom.tfaces and len(geom.tfaces) == num_faces:
            self._apply_uvs(mesh, geom)

        # Vertex colors
        if (self.options.get('import_vertex_colors', True)
                and geom.cvertices and geom.cfaces
                and len(geom.cfaces) == num_faces):
            self._apply_vertex_colors(mesh, geom)

        # Smoothing groups -> sharp edges
        if (self.options.get('import_smoothing', True)
                and geom.smoothing_groups
                and len(geom.smoothing_groups) == num_faces):
            self._apply_smoothing_groups(mesh, geom)

        # Custom normals (apply after smoothing / sharp edges are set)
        if (self.options.get('import_normals', True)
                and geom.vertex_normals
                and len(geom.vertex_normals) == num_faces):
            self._apply_normals(mesh, geom)

        mesh.update()

        # Create Blender object, link to active collection
        obj = bpy.data.objects.new(geom.name, mesh)
        bpy.context.collection.objects.link(obj)

        print(f'  "{geom.name}": {len(verts)} verts, {num_faces} tris, '
              f'mat_ref={geom.material_ref}')
        return obj

    # -- UVs -----------------------------------------------------------------

    def _apply_uvs(self, mesh, geom):
        uv_layer = mesh.uv_layers.new(name='UVMap')
        uv_data = uv_layer.data

        for fi, poly in enumerate(mesh.polygons):
            if fi >= len(geom.tfaces):
                break
            tf = geom.tfaces[fi]
            for li, loop_idx in enumerate(poly.loop_indices):
                if li < 3:
                    tv_idx = tf[li]
                    if tv_idx < len(geom.tvertices):
                        uv_data[loop_idx].uv = geom.tvertices[tv_idx]

    # -- Vertex colors -------------------------------------------------------

    def _apply_vertex_colors(self, mesh, geom):
        vc = mesh.color_attributes.new(
            name='Col', type='FLOAT_COLOR', domain='CORNER')

        for fi, poly in enumerate(mesh.polygons):
            if fi >= len(geom.cfaces):
                break
            cf = geom.cfaces[fi]
            for li, loop_idx in enumerate(poly.loop_indices):
                if li < 3:
                    cv_idx = cf[li]
                    if cv_idx < len(geom.cvertices):
                        r, g, b = geom.cvertices[cv_idx]
                        vc.data[loop_idx].color = (r, g, b, 1.0)

    # -- Smoothing groups -> sharp edges -------------------------------------

    def _apply_smoothing_groups(self, mesh, geom):
        """Mark edges as sharp where adjacent faces have different
        smoothing groups."""
        sg_list = geom.smoothing_groups

        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.faces.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        for edge in bm.edges:
            linked = edge.link_faces
            if len(linked) == 2:
                sg0 = sg_list[linked[0].index] if linked[0].index < len(sg_list) else 0
                sg1 = sg_list[linked[1].index] if linked[1].index < len(sg_list) else 0
                # Edge is sharp if smoothing groups differ or either is 0
                if sg0 == 0 or sg1 == 0 or sg0 != sg1:
                    edge.smooth = False
                else:
                    edge.smooth = True
            # Boundary edges keep Blender default (smooth)

        bm.to_mesh(mesh)
        bm.free()

    # -- Custom normals ------------------------------------------------------

    def _apply_normals(self, mesh, geom):
        """Set per-loop custom split normals from parsed vertex normals."""
        num_loops = len(mesh.loops)
        normals = [(0.0, 0.0, 1.0)] * num_loops

        for fi, poly in enumerate(mesh.polygons):
            if fi >= len(geom.vertex_normals):
                break
            vn = geom.vertex_normals[fi]
            for li, loop_idx in enumerate(poly.loop_indices):
                if li < len(vn):
                    normals[loop_idx] = vn[li]

        mesh.normals_split_custom_set(normals)


# =============================================================================
# Blender Operator
# =============================================================================

class ImportASE(bpy.types.Operator, ImportHelper):
    """Import ASCII Scene Export (.ase) models for idTech 4"""
    bl_idname = "import_scene.ase"
    bl_label = "Import ASE"
    bl_options = {'PRESET', 'UNDO'}
    filename_ext = ".ase"
    filter_glob: StringProperty(default="*.ase;*.ASE", options={'HIDDEN'})

    filepath: StringProperty(
        name="File Path",
        description="Path to the .ase file",
        maxlen=1024,
        default="",
    )

    option_scale: FloatProperty(
        name="Scale",
        description=(
            "Scale factor for imported geometry. "
            "Default 1.0 imports at native ASE coordinates"
        ),
        min=0.001,
        max=10000.0,
        soft_min=0.01,
        soft_max=1000.0,
        default=1.0,
    )

    option_import_normals: BoolProperty(
        name="Import custom normals",
        description=(
            "Set per-face vertex normals from the ASE file. "
            "Preserves exact shading from the original model"
        ),
        default=True,
    )

    option_import_vertex_colors: BoolProperty(
        name="Import vertex colors",
        description="Import vertex color data if present in the ASE file",
        default=True,
    )

    option_import_smoothing: BoolProperty(
        name="Import smoothing groups",
        description=(
            "Mark edges as sharp where adjacent faces have different "
            "smoothing groups, preserving hard/soft shading transitions"
        ),
        default=True,
    )

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text='Transform:')
        box.prop(self, 'option_scale')

        box = layout.box()
        box.label(text='Data:')
        box.prop(self, 'option_import_normals')
        box.prop(self, 'option_import_vertex_colors')
        box.prop(self, 'option_import_smoothing')

    def execute(self, context):
        start = time.perf_counter()

        if not os.path.isfile(self.filepath):
            self.report({'ERROR'}, f'File not found: {self.filepath}')
            return {'CANCELLED'}

        print(f'\nASE Import: {self.filepath}')

        try:
            with open(self.filepath, 'r', errors='replace') as f:
                text = f.read()
        except IOError as e:
            self.report({'ERROR'}, f'Could not read file: {e}')
            return {'CANCELLED'}

        # Parse
        parser = ASEParser(text)
        parser.parse()

        if not parser.objects:
            self.report({'ERROR'}, 'No GEOMOBJECT found in ASE file')
            return {'CANCELLED'}

        print(f'  Parsed: {len(parser.materials)} material(s), '
              f'{len(parser.objects)} object(s)')

        # Build Blender objects
        options = {
            'scale': self.option_scale,
            'import_normals': self.option_import_normals,
            'import_vertex_colors': self.option_import_vertex_colors,
            'import_smoothing': self.option_import_smoothing,
        }
        builder = ASEMeshBuilder(parser, options)
        objects = builder.build()

        # Select imported objects
        bpy.ops.object.select_all(action='DESELECT')
        for obj in objects:
            obj.select_set(True)
        if objects:
            bpy.context.view_layer.objects.active = objects[0]

        elapsed = time.perf_counter() - start
        self.report({'INFO'},
                    f'Imported {len(objects)} object(s) in {elapsed:.3f}s')
        print(f'ASE Import: Done in {elapsed:.3f}s\n')

        return {'FINISHED'}


# =============================================================================
# Registration
# =============================================================================

def menu_func_import(self, context):
    self.layout.operator(
        ImportASE.bl_idname, text="ASCII Scene Export (.ase)")


def register():
    bpy.utils.register_class(ImportASE)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.utils.unregister_class(ImportASE)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)


if __name__ == "__main__":
    register()
