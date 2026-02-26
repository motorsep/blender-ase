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
    "name": "ASE Exporter for idTech 4",
    "author": "Richard Bartlett, MCampagnini, scorpion81, motorsep/Claude",
    "version": (3, 0, 0),
    "blender": (4, 2, 0),
    "location": "File > Export > ASCII Scene Export (.ase)",
    "description": "Export static meshes to ASCII Scene Export (.ase) format for idTech 4",
    "warning": "",
    "wiki_url": "",
    "tracker_url": "",
    "category": "Import-Export",
}

"""
ASE Exporter for idTech 4 (Doom 3, Quake 4, Prey, Dark Mod, etc.)

Exports selected mesh objects as .ase files compatible with idTech 4 engine.
Supports multiple materials, vertex colors, smoothing groups, and UV channels.

Non-destructive: all operations work on evaluated copies of the mesh data,
the original scene and objects are never modified.
"""

import os
import bpy
import bmesh
import time
from bpy_extras.io_utils import ExportHelper
from bpy.props import StringProperty, BoolProperty, FloatProperty


# =============================================================================
# Formatting helpers
# =============================================================================

def ase_float(x):
    """Format a float to 4 decimal places with ASE-style spacing."""
    return f'{x: 0.4f}'


def ase_color(r, g, b):
    """Format an RGB color triplet."""
    return f'{ase_float(r)}\t{ase_float(g)}\t{ase_float(b)}'


# =============================================================================
# Material property extraction (Blender 4.x node-based)
# =============================================================================

def find_principled(mat):
    """Find the Principled BSDF node in a material's node tree."""
    if mat and mat.node_tree:
        for node in mat.node_tree.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                return node
    return None


def get_diffuse_color(mat):
    principled = find_principled(mat)
    if principled:
        col = principled.inputs['Base Color'].default_value
        return (col[0], col[1], col[2])
    return (0.8, 0.8, 0.8)


def get_specular_color(mat):
    principled = find_principled(mat)
    if principled:
        spec = principled.inputs['Specular IOR Level'].default_value
        return (spec, spec, spec)
    return (1.0, 1.0, 1.0)


def get_shine(mat):
    principled = find_principled(mat)
    if principled:
        roughness = principled.inputs['Roughness'].default_value
        return (1.0 - roughness) ** 2
    return 0.1


def get_shine_strength(mat):
    principled = find_principled(mat)
    if principled:
        return principled.inputs['Specular IOR Level'].default_value
    return 1.0


def get_transparency(mat):
    principled = find_principled(mat)
    if principled:
        return 1.0 - principled.inputs['Alpha'].default_value
    return 0.0


def get_selfillum(mat):
    principled = find_principled(mat)
    if principled:
        return principled.inputs['Emission Strength'].default_value
    return 0.0


def get_bitmap_path(mat):
    """Get the diffuse texture path from the material's node tree.

    Returns the material name as the bitmap path (idTech 4 convention:
    material name IS the texture/shader path). Falls back to 'None'."""
    if mat:
        return '\\\\base\\' + mat.name.replace('/', '\\')
    return 'None'


# =============================================================================
# Smoothing group computation (non-destructive, bmesh-based)
# =============================================================================

def compute_smoothing_groups(bm):
    """Compute smoothing groups from sharp edges using flood-fill on a bmesh.

    Faces connected through non-sharp edges belong to the same smoothing group.
    Returns a dict mapping face index -> smoothing group ID (1-based, mod 32).
    """
    bm.faces.ensure_lookup_table()
    bm.edges.ensure_lookup_table()

    visited = set()
    groups = {}
    group_id = 0

    for face in bm.faces:
        if face.index in visited:
            continue

        # Flood fill from this face across non-sharp edges
        group_id += 1
        stack = [face]
        while stack:
            f = stack.pop()
            if f.index in visited:
                continue
            visited.add(f.index)
            groups[f.index] = group_id

            for edge in f.edges:
                if edge.smooth:  # not sharp
                    for linked_face in edge.link_faces:
                        if linked_face.index not in visited:
                            stack.append(linked_face)

    return groups


def mesh_needs_smoothing_groups(obj):
    """Determine if a mesh needs smoothing groups exported.

    Returns True if the mesh has any sharp edges, flat-shaded faces,
    auto-smooth enabled, or relevant modifiers (Smooth by Angle, Edge Split).
    Returns False if all faces are smooth-shaded with no sharp edges.
    """
    mesh = obj.data

    # Check for flat-shaded faces
    for poly in mesh.polygons:
        if not poly.use_smooth:
            return True

    # Check for sharp edges
    for edge in mesh.edges:
        if not edge.smooth:
            return True

    # Check for auto-smooth (Blender 4.x: attribute-based)
    if hasattr(mesh, 'has_custom_normals') and mesh.has_custom_normals:
        return True

    # Check for Edge Split or Smooth by Angle modifiers
    for mod in obj.modifiers:
        if mod.type in ('EDGE_SPLIT', 'NODES'):
            # NODES could be "Smooth by Angle" geometry nodes modifier
            if mod.type == 'NODES' and mod.node_group:
                if 'smooth' in mod.node_group.name.lower():
                    return True
            else:
                return True

    return False


# =============================================================================
# ASE data building (string assembly using lists for performance)
# =============================================================================

class ASEBuilder:
    """Builds complete ASE file content from Blender scene data.

    All mesh operations are non-destructive: we work on evaluated copies
    obtained via depsgraph, never modifying the original objects.
    """

    def __init__(self, context, options):
        self.context = context
        self.options = options
        self.material_list = []  # ordered list of unique materials
        self.mat_name_to_index = {}  # material name -> index in material_list

    def build(self, objects):
        """Build complete ASE content for the given mesh objects.

        Returns a string containing the full ASE file.
        """
        # Collect all unique materials from all selected mesh objects
        self._collect_materials(objects)

        parts = []
        parts.append(self._build_header())
        parts.append(self._build_scene())
        parts.append(self._build_materials())

        for obj in objects:
            parts.append(self._build_geomobject(obj))

        return ''.join(parts)

    def build_split(self, obj):
        """Build separate ASE files for each material on an object.

        Returns a list of (suffix, ase_content) tuples.
        Vertex colors and normals are preserved per-chunk so that
        chunks loaded side-by-side appear as a continuous mesh.
        """
        self._collect_materials([obj])

        results = []
        mesh, bm = self._get_evaluated_bmesh(obj)
        xform = self._compute_transform_matrix(obj)

        # Find which material indices are actually used
        used_mat_indices = sorted(set(f.material_index for f in bm.faces))

        for chunk_idx, mat_idx in enumerate(used_mat_indices):
            # Clone bmesh and remove faces not belonging to this material
            bm_chunk = bm.copy()
            faces_to_remove = [f for f in bm_chunk.faces if f.material_index != mat_idx]
            for f in faces_to_remove:
                bm_chunk.faces.remove(f)

            # Clean up: remove orphan verts/edges
            verts_to_remove = [v for v in bm_chunk.verts if not v.link_faces]
            for v in verts_to_remove:
                bm_chunk.verts.remove(v)

            if len(bm_chunk.faces) == 0:
                bm_chunk.free()
                continue

            # Convert chunk bmesh to a temporary mesh for export
            chunk_mesh = bpy.data.meshes.new(f'_ase_chunk_{chunk_idx}')
            bm_chunk.to_mesh(chunk_mesh)
            bm_chunk.free()

            # Triangulate if needed
            if self.options['triangulate']:
                self._triangulate_mesh(chunk_mesh)

            # Build ASE for this chunk
            chunk_name = f'{obj.name}_chunk{chunk_idx:03d}'
            parts = []
            parts.append(self._build_header())
            parts.append(self._build_scene())

            # Single material for this chunk
            if mat_idx < len(obj.material_slots) and obj.material_slots[mat_idx].material:
                mat = obj.material_slots[mat_idx].material
                # Find global material index
                global_mat_idx = self.mat_name_to_index.get(mat.name, 0)
            else:
                global_mat_idx = 0

            parts.append(self._build_materials())
            parts.append(self._build_geomobject_from_mesh(
                chunk_name, chunk_mesh, obj, global_mat_idx, xform=xform))

            bpy.data.meshes.remove(chunk_mesh)

            suffix = f'_chunk{chunk_idx:03d}'
            results.append((suffix, ''.join(parts)))

        bm.free()
        bpy.data.meshes.remove(mesh)

        return results

    # -------------------------------------------------------------------------
    # Material collection
    # -------------------------------------------------------------------------

    def _collect_materials(self, objects):
        """Collect all unique materials from the given objects."""
        self.material_list = []
        self.mat_name_to_index = {}

        for obj in objects:
            if obj.type != 'MESH':
                continue
            for slot in obj.material_slots:
                if slot.material and slot.material.name not in self.mat_name_to_index:
                    self.mat_name_to_index[slot.material.name] = len(self.material_list)
                    self.material_list.append(slot.material)

        if not self.material_list:
            raise RuntimeError('Selected meshes must have at least one material assigned')

    # -------------------------------------------------------------------------
    # Header & Scene
    # -------------------------------------------------------------------------

    def _build_header(self):
        return '*3DSMAX_ASCIIEXPORT\t200\n*COMMENT "ASE Exporter for idTech 4 - Blender"\n'

    def _build_scene(self):
        filename = bpy.data.filepath or 'untitled.blend'
        return (
            f'*SCENE {{\n'
            f'\t*SCENE_FILENAME "{filename}"\n'
            f'\t*SCENE_FIRSTFRAME 0\n'
            f'\t*SCENE_LASTFRAME 100\n'
            f'\t*SCENE_FRAMESPEED 30\n'
            f'\t*SCENE_TICKSPERFRAME 160\n'
            f'\t*SCENE_BACKGROUND_STATIC 0.0000\t0.0000\t0.0000\n'
            f'\t*SCENE_AMBIENT_STATIC 0.0000\t0.0000\t0.0000\n'
            f'}}\n'
        )

    # -------------------------------------------------------------------------
    # Materials
    # -------------------------------------------------------------------------

    def _build_materials(self):
        lines = []
        count = len(self.material_list)
        lines.append(f'*MATERIAL_LIST {{\n')
        lines.append(f'\t*MATERIAL_COUNT {count}\n')

        for idx, mat in enumerate(self.material_list):
            lines.append(self._build_single_material(idx, mat))

        lines.append(f'}}\n')
        return ''.join(lines)

    def _build_single_material(self, index, mat):
        diffuse = get_diffuse_color(mat)
        specular = get_specular_color(mat)
        bitmap = get_bitmap_path(mat)

        return (
            f'\t*MATERIAL {index} {{\n'
            f'\t\t*MATERIAL_NAME "{mat.name}"\n'
            f'\t\t*MATERIAL_CLASS "Standard"\n'
            f'\t\t*MATERIAL_AMBIENT {ase_color(0.0, 0.0, 0.0)}\n'
            f'\t\t*MATERIAL_DIFFUSE {ase_color(*diffuse)}\n'
            f'\t\t*MATERIAL_SPECULAR {ase_color(*specular)}\n'
            f'\t\t*MATERIAL_SHINE {ase_float(get_shine(mat))}\n'
            f'\t\t*MATERIAL_SHINESTRENGTH {ase_float(get_shine_strength(mat))}\n'
            f'\t\t*MATERIAL_TRANSPARENCY {ase_float(get_transparency(mat))}\n'
            f'\t\t*MATERIAL_WIRESIZE {ase_float(1.0)}\n'
            f'\t\t*MATERIAL_SHADING Phong\n'
            f'\t\t*MATERIAL_XP_FALLOFF {ase_float(0.0)}\n'
            f'\t\t*MATERIAL_SELFILLUM {ase_float(get_selfillum(mat))}\n'
            f'\t\t*MATERIAL_FALLOFF In\n'
            f'\t\t*MATERIAL_XP_TYPE Filter\n'
            f'\t\t*MAP_DIFFUSE {{\n'
            f'\t\t\t*MAP_NAME "{mat.name}"\n'
            f'\t\t\t*MAP_CLASS "Bitmap"\n'
            f'\t\t\t*MAP_SUBNO 1\n'
            f'\t\t\t*MAP_AMOUNT {ase_float(1.0)}\n'
            f'\t\t\t*BITMAP "{bitmap}"\n'
            f'\t\t\t*MAP_TYPE Screen\n'
            f'\t\t\t*UVW_U_OFFSET {ase_float(0.0)}\n'
            f'\t\t\t*UVW_V_OFFSET {ase_float(0.0)}\n'
            f'\t\t\t*UVW_U_TILING {ase_float(1.0)}\n'
            f'\t\t\t*UVW_V_TILING {ase_float(1.0)}\n'
            f'\t\t\t*UVW_ANGLE {ase_float(0.0)}\n'
            f'\t\t\t*UVW_BLUR {ase_float(1.0)}\n'
            f'\t\t\t*UVW_BLUR_OFFSET {ase_float(0.0)}\n'
            f'\t\t\t*UVW_NOUSE_AMT {ase_float(1.0)}\n'
            f'\t\t\t*UVW_NOISE_SIZE {ase_float(1.0)}\n'
            f'\t\t\t*UVW_NOISE_LEVEL 1\n'
            f'\t\t\t*UVW_NOISE_PHASE {ase_float(0.0)}\n'
            f'\t\t\t*BITMAP_FILTER Pyramidal\n'
            f'\t\t}}\n'
            f'\t}}\n'
        )

    # -------------------------------------------------------------------------
    # Geometry object
    # -------------------------------------------------------------------------

    def _compute_transform_matrix(self, obj):
        """Compute the transform matrix to bake into vertex positions.

        Builds a matrix from the object's world matrix components based on
        which transform options are enabled. This is fully non-destructive.
        """
        import mathutils

        mat = mathutils.Matrix.Identity(4)
        loc, rot, scl = obj.matrix_world.decompose()

        if self.options.get('apply_scale', True):
            mat = mathutils.Matrix.Diagonal((*scl, 1.0)) @ mat
        if self.options.get('apply_rotation', True):
            mat = rot.to_matrix().to_4x4() @ mat
        if self.options.get('apply_location', True):
            loc_mat = mathutils.Matrix.Translation(loc)
            mat = loc_mat @ mat

        return mat

    def _get_evaluated_bmesh(self, obj):
        """Get a triangulated bmesh from the evaluated (modifier-applied) object.

        Returns (mesh, bmesh) - caller must free both when done.
        """
        depsgraph = self.context.evaluated_depsgraph_get()
        obj_eval = obj.evaluated_get(depsgraph)
        mesh = bpy.data.meshes.new_from_object(obj_eval)

        if self.options['triangulate']:
            self._triangulate_mesh(mesh)

        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        return mesh, bm

    def _triangulate_mesh(self, mesh):
        """Triangulate a mesh in-place using bmesh."""
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        bm.to_mesh(mesh)
        bm.free()

    def _build_geomobject(self, obj):
        """Build a GEOMOBJECT block for a Blender object."""
        mesh, bm = self._get_evaluated_bmesh(obj)
        xform = self._compute_transform_matrix(obj)
        result = self._build_geomobject_from_data(obj.name, mesh, bm, obj, xform=xform)
        bm.free()
        bpy.data.meshes.remove(mesh)
        return result

    def _build_geomobject_from_mesh(self, name, mesh, obj, material_ref, xform=None):
        """Build a GEOMOBJECT from an already-prepared mesh object."""
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        result = self._build_geomobject_from_data(
            name, mesh, bm, obj, material_ref, xform=xform)
        bm.free()
        return result

    def _build_geomobject_from_data(self, name, mesh, bm, obj,
                                     material_ref_override=None, xform=None):
        """Core geometry builder working from mesh data + bmesh.

        This is the main workhorse that generates the GEOMOBJECT block.
        It writes vertices, faces, UVs, vertex colors, and normals.
        """
        scale = self.options['scale']
        use_smoothing = self.options['smoothing_groups']

        # Validate all faces are triangles
        for face in bm.faces:
            if len(face.verts) != 3:
                raise RuntimeError(
                    f'Mesh "{name}" contains non-triangulated faces. '
                    f'Enable "Triangulate" in export options.')

        num_verts = len(bm.verts)
        num_faces = len(bm.faces)

        # Compute smoothing groups if needed
        smoothing_groups = {}
        if use_smoothing:
            smoothing_groups = compute_smoothing_groups(bm)

        # Determine material ref (for the GEOMOBJECT's MATERIAL_REF)
        # With a flat material list, MATERIAL_REF is 0 and per-face
        # MESH_MTLID indexes directly into the global material list.
        # When split-per-material provides an override, use that instead.
        if material_ref_override is not None:
            mat_ref = material_ref_override
        else:
            mat_ref = 0

        lines = []
        lines.append(f'*GEOMOBJECT {{\n')
        lines.append(f'\t*NODE_NAME "{name}"\n')

        # NODE_TM (identity transform - transforms are baked)
        lines.append(self._build_node_tm(name))

        # MESH block
        lines.append(f'\t*MESH {{\n')
        lines.append(f'\t\t*TIMEVALUE 0\n')
        lines.append(f'\t\t*MESH_NUMVERTEX {num_verts}\n')
        lines.append(f'\t\t*MESH_NUMFACES {num_faces}\n')

        # Vertex list (with transform and scale applied)
        import mathutils
        if xform is None:
            xform = mathutils.Matrix.Identity(4)
        # Normal transform is inverse-transpose of the upper 3x3
        normal_xform = xform.to_3x3().inverted_safe().transposed()

        lines.append(f'\t\t*MESH_VERTEX_LIST {{\n')
        for v in bm.verts:
            co = xform @ v.co
            x = ase_float(co.x * scale)
            y = ase_float(co.y * scale)
            z = ase_float(co.z * scale)
            lines.append(f'\t\t\t*MESH_VERTEX {v.index}\t{x}\t{y}\t{z}\n')
        lines.append(f'\t\t}}\n')

        # Face list
        lines.append(f'\t\t*MESH_FACE_LIST {{\n')
        for face in bm.faces:
            v = face.verts
            # Material ID for this face
            mat_idx = face.material_index
            if mat_idx < len(obj.material_slots) and obj.material_slots[mat_idx].material:
                mat_name = obj.material_slots[mat_idx].material.name
                mtl_id = self.mat_name_to_index.get(mat_name, 0)
            else:
                mtl_id = 0

            # Smoothing group
            sg = smoothing_groups.get(face.index, 0)
            if sg > 0:
                sg_val = ((sg - 1) % 32) + 1
                sg_str = str(sg_val)
            else:
                sg_str = '0'

            # Edge visibility (AB, BC, CA) - 1 if edge is not shared
            edges = face.edges
            ab = 1 if len(edges[0].link_faces) == 1 else 0
            bc = 1 if len(edges[1].link_faces) == 1 else 0
            ca = 1 if len(edges[2].link_faces) == 1 else 0

            lines.append(
                f'\t\t\t*MESH_FACE {face.index}:'
                f'    A: {v[0].index:>5} B: {v[1].index:>5} C: {v[2].index:>5}'
                f' AB: {ab:>4} BC: {bc:>4} CA: {ca:>4}'
                f'\t *MESH_SMOOTHING {sg_str}'
                f' \t*MESH_MTLID {mtl_id}\n'
            )
        lines.append(f'\t\t}}\n')

        # UV coordinates (per-face-vertex, i.e. per loop)
        uv_layers = mesh.uv_layers
        if uv_layers and len(uv_layers) > 0:
            # Primary UV channel
            active_uv = uv_layers.active
            if active_uv:
                num_tverts = num_faces * 3
                lines.append(f'\t\t*MESH_NUMTVERTEX {num_tverts}\n')
                lines.append(f'\t\t*MESH_TVERTLIST {{\n')

                tvert_idx = 0
                for poly in mesh.polygons:
                    for loop_idx in poly.loop_indices:
                        uv = active_uv.data[loop_idx].uv
                        lines.append(
                            f'\t\t\t*MESH_TVERT {tvert_idx}\t'
                            f'{ase_float(uv.x)}\t{ase_float(uv.y)}\t{ase_float(0.0)}\n')
                        tvert_idx += 1
                lines.append(f'\t\t}}\n')

                lines.append(f'\t\t*MESH_NUMTVFACES {num_faces}\n')
                lines.append(f'\t\t*MESH_TFACELIST {{\n')
                for fi in range(num_faces):
                    base = fi * 3
                    lines.append(
                        f'\t\t\t*MESH_TFACE {fi}\t{base}\t{base + 1}\t{base + 2}\n')
                lines.append(f'\t\t}}\n')

            # Additional UV mapping channels (channel 2+)
            for ch_idx in range(1, len(uv_layers)):
                uv_layer = uv_layers[ch_idx]
                channel_num = ch_idx + 1
                num_tverts = num_faces * 3
                lines.append(f'\t\t*MESH_MAPPINGCHANNEL {channel_num} {{\n')
                lines.append(f'\t\t\t*MESH_NUMTVERTEX {num_tverts}\n')
                lines.append(f'\t\t\t*MESH_TVERTLIST {{\n')

                tvert_idx = 0
                for poly in mesh.polygons:
                    for loop_idx in poly.loop_indices:
                        uv = uv_layer.data[loop_idx].uv
                        lines.append(
                            f'\t\t\t\t*MESH_TVERT {tvert_idx}\t'
                            f'{ase_float(uv.x)}\t{ase_float(uv.y)}\t{ase_float(0.0)}\n')
                        tvert_idx += 1
                lines.append(f'\t\t\t}}\n')

                lines.append(f'\t\t\t*MESH_NUMTVFACES {num_faces}\n')
                lines.append(f'\t\t\t*MESH_TFACELIST {{\n')
                for fi in range(num_faces):
                    base = fi * 3
                    lines.append(
                        f'\t\t\t\t*MESH_TFACE {fi}\t{base}\t{base + 1}\t{base + 2}\n')
                lines.append(f'\t\t\t}}\n')
                lines.append(f'\t\t}}\n')

        # Vertex colors
        vc_layer = None
        if mesh.color_attributes:
            # Prefer the active color attribute
            vc_layer = mesh.color_attributes.active_color
        # Fallback for older API
        if vc_layer is None and hasattr(mesh, 'vertex_colors') and mesh.vertex_colors:
            vc_layer = mesh.vertex_colors.active

        if vc_layer is not None:
            num_cverts = num_faces * 3
            lines.append(f'\t\t*MESH_NUMCVERTEX {num_cverts}\n')
            lines.append(f'\t\t*MESH_CVERTLIST {{\n')

            cvert_idx = 0
            for poly in mesh.polygons:
                for loop_idx in poly.loop_indices:
                    if hasattr(vc_layer, 'data') and len(vc_layer.data) > loop_idx:
                        color = vc_layer.data[loop_idx].color
                    else:
                        color = (1.0, 1.0, 1.0, 1.0)
                    lines.append(
                        f'\t\t\t*MESH_VERTCOL {cvert_idx}\t'
                        f'{ase_float(color[0])}\t{ase_float(color[1])}\t{ase_float(color[2])}\n')
                    cvert_idx += 1
            lines.append(f'\t\t}}\n')

            lines.append(f'\t\t*MESH_NUMCVFACES {num_faces}\n')
            lines.append(f'\t\t*MESH_CFACELIST {{\n')
            for fi in range(num_faces):
                base = fi * 3
                lines.append(
                    f'\t\t\t*MESH_CFACE {fi}\t{base}\t{base + 1}\t{base + 2}\n')
            lines.append(f'\t\t}}\n')

        # Normals - use split normals (per-loop) for correct hard/soft edge export
        lines.append(f'\t\t*MESH_NORMALS {{\n')

        # Try to use split normals for accurate per-loop normals
        use_split_normals = False
        if hasattr(mesh, 'calc_normals_split'):
            mesh.calc_normals_split()
            use_split_normals = True

        for poly in mesh.polygons:
            fn = (normal_xform @ poly.normal).normalized()
            lines.append(
                f'\t\t\t*MESH_FACENORMAL {poly.index}\t'
                f'{ase_float(fn.x)}\t{ase_float(fn.y)}\t{ase_float(fn.z)}\n')

            for loop_idx in poly.loop_indices:
                vert_idx = mesh.loops[loop_idx].vertex_index
                if use_split_normals:
                    raw_n = mesh.loops[loop_idx].normal
                else:
                    raw_n = mesh.vertices[vert_idx].normal
                n = (normal_xform @ raw_n).normalized()
                lines.append(
                    f'\t\t\t\t*MESH_VERTEXNORMAL {vert_idx}\t'
                    f'{ase_float(n.x)}\t{ase_float(n.y)}\t{ase_float(n.z)}\n')

        lines.append(f'\t\t}}\n')

        # Close MESH block
        lines.append(f'\t}}\n')

        # Properties
        lines.append(f'\t*PROP_MOTIONBLUR 0\n')
        lines.append(f'\t*PROP_CASTSHADOW 1\n')
        lines.append(f'\t*PROP_RECVSHADOW 1\n')
        lines.append(f'\t*MATERIAL_REF {mat_ref}\n')

        # Close GEOMOBJECT
        lines.append(f'}}\n')

        return ''.join(lines)

    def _build_node_tm(self, name):
        """Build an identity NODE_TM block. Transforms are baked into vertex data."""
        return (
            f'\t*NODE_TM {{\n'
            f'\t\t*NODE_NAME "{name}"\n'
            f'\t\t*INHERIT_POS 0 0 0\n'
            f'\t\t*INHERIT_ROT 0 0 0\n'
            f'\t\t*INHERIT_SCL 0 0 0\n'
            f'\t\t*TM_ROW0 1.0000\t0.0000\t0.0000\n'
            f'\t\t*TM_ROW1 0.0000\t1.0000\t0.0000\n'
            f'\t\t*TM_ROW2 0.0000\t0.0000\t1.0000\n'
            f'\t\t*TM_ROW3 0.0000\t0.0000\t0.0000\n'
            f'\t\t*TM_POS 0.0000\t0.0000\t0.0000\n'
            f'\t\t*TM_ROTAXIS 0.0000\t0.0000\t0.0000\n'
            f'\t\t*TM_ROTANGLE 0.0000\n'
            f'\t\t*TM_SCALE 1.0000\t1.0000\t1.0000\n'
            f'\t\t*TM_SCALEAXIS 0.0000\t0.0000\t0.0000\n'
            f'\t\t*TM_SCALEAXISANG 0.0000\n'
            f'\t}}\n'
        )


# =============================================================================
# Blender Operator
# =============================================================================

class ExportASE(bpy.types.Operator, ExportHelper):
    """Export selected meshes to ASCII Scene Export (.ase) format for idTech 4"""
    bl_idname = "export_scene.ase"
    bl_label = "Export ASE"
    bl_options = {'PRESET'}
    filename_ext = ".ase"
    filter_glob: StringProperty(default="*.ase", options={'HIDDEN'})

    filepath: StringProperty(
        name="File Path",
        description="Output file path",
        maxlen=1024,
        default="",
    )

    # -- Essentials --

    option_triangulate: BoolProperty(
        name="Triangulate",
        description="Triangulate all meshes (required if mesh has quads/ngons)",
        default=True,
    )

    # -- Transformations --

    option_apply_scale: BoolProperty(
        name="Apply Scale",
        description="Bake object scale into vertex positions",
        default=True,
    )

    option_apply_rotation: BoolProperty(
        name="Apply Rotation",
        description="Bake object rotation into vertex positions",
        default=True,
    )

    option_apply_location: BoolProperty(
        name="Apply Location",
        description="Bake object location into vertex positions",
        default=True,
    )

    # -- Advanced --

    option_scale: FloatProperty(
        name="Scale",
        description=(
            "Multiply all vertex positions by this factor. "
            "idTech 4 uses roughly 1 unit = 1 inch. "
            "Default 16.0 scales 1 Blender unit (1m) to ~16 Doom units"
        ),
        min=0.001,
        max=10000.0,
        soft_min=0.01,
        soft_max=1000.0,
        default=1.0,
    )

    option_separate: BoolProperty(
        name="Separate files per object",
        description="Write a separate .ase file for each selected object",
        default=False,
    )

    option_split_per_material: BoolProperty(
        name="Split per material",
        description=(
            "Split each object into separate ASE files by material. "
            "Output files are named <object>_chunk000.ase, _chunk001.ase, etc. "
            "Vertex colors and normals are preserved for seamless appearance"
        ),
        default=False,
    )

    option_smoothing_groups: BoolProperty(
        name="Smoothing Groups",
        description=(
            "Export smoothing groups based on sharp edges. "
            "Auto-detected: if mesh is fully smooth with no sharp edges, "
            "a single smoothing group is used"
        ),
        default=True,
    )

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text='Essentials:')
        box.prop(self, 'option_triangulate')

        box = layout.box()
        box.label(text='Transformations:')
        box.prop(self, 'option_apply_scale')
        box.prop(self, 'option_apply_rotation')
        box.prop(self, 'option_apply_location')

        box = layout.box()
        box.label(text='Advanced:')
        box.prop(self, 'option_scale')
        box.prop(self, 'option_separate')
        box.prop(self, 'option_split_per_material')
        box.prop(self, 'option_smoothing_groups')

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        start = time.perf_counter()

        # Collect selected mesh objects
        mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not mesh_objects:
            self.report({'ERROR'}, 'No mesh objects selected')
            return {'CANCELLED'}

        print(f'\nASE Export: {len(mesh_objects)} mesh object(s) selected')

        # Build a transform matrix per object to bake into vertex positions.
        # This is fully non-destructive: we never modify the original objects.

        options = {
            'scale': self.option_scale,
            'triangulate': self.option_triangulate,
            'smoothing_groups': self.option_smoothing_groups,
            'apply_location': self.option_apply_location,
            'apply_rotation': self.option_apply_rotation,
            'apply_scale': self.option_apply_scale,
        }

        try:
            builder = ASEBuilder(context, options)

            if self.option_split_per_material:
                # Split mode: each object produces multiple chunk files
                for obj in mesh_objects:
                    chunks = builder.build_split(obj)
                    base_dir = os.path.dirname(self.filepath)

                    for suffix, ase_content in chunks:
                        chunk_path = os.path.join(
                            base_dir, f'{obj.name}{suffix}.ase')
                        self._write_file(chunk_path, ase_content)

            elif self.option_separate:
                # Separate mode: one file per object
                base_dir = os.path.dirname(self.filepath)
                for obj in mesh_objects:
                    ase_content = builder.build([obj])
                    filename = os.path.join(
                        base_dir, obj.name.replace('.', '_') + '.ase')
                    self._write_file(filename, ase_content)

            else:
                # Normal mode: all objects in one file
                ase_content = builder.build(mesh_objects)
                self._write_file(self.filepath, ase_content)

        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        elapsed = time.perf_counter() - start
        print(f'ASE Export completed in {elapsed:.3f}s')
        self.report({'INFO'}, f'ASE export completed in {elapsed:.3f}s')

        return {'FINISHED'}

    def _write_file(self, filepath, data):
        """Write ASE data string to file."""
        print(f'Writing: {filepath}')
        try:
            with open(filepath, 'w', newline='\n') as f:
                f.write(data)
        except IOError as e:
            raise RuntimeError(f'Could not write file: {filepath}\n{e}')


# =============================================================================
# Registration
# =============================================================================

def menu_func_export(self, context):
    self.layout.operator(ExportASE.bl_idname, text="ASCII Scene Export (.ase)")


def register():
    bpy.utils.register_class(ExportASE)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.utils.unregister_class(ExportASE)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)


if __name__ == "__main__":
    register()
