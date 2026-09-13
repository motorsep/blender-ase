# blender-ase
io_import_idt4ase - ASE import add-on for Blender 4.5+

io_export_idt4ase - refactored and optimized ASE export add-on for Blender 4.5+

What's new:
- much faster than the old ASE export add-on
- splits mesh into multiple meshes per material, while retaining vertex colors and vertex normals on the split boundary
- exports multi-material meshes correctly
- exports LOD groups (LODed meshes to work with StormEngine2 and RBDoom 3 BFG)
- batch export (exports multiple meshes at one, including LODed meshes and split meshes)
- MikkT option: writes a *MESH_TANGENTS block with MikkTSpace tangents computed against the exported split normals (Fall of Phaeton engine; stock idTech 4 skips the block)
- MultiUV option: each material samples the UV map named by the UV Map node in its node tree, so one mesh can carry e.g. a body map and a GUI screen map. Setup and rules in [MULTIUV.md](MULTIUV.md)

io_export_ase - ASE Exporter for Blender 3.4.1

ASE258.py - Original ASE Exporter for Blender 2.76+
