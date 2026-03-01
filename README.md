# blender-ase
io_export_idt4ase - refactored and optimized ASE export add-on to work with Blender 4.5+

What's new:
- much faster than the old ASE export add-on
- splits mesh into multiple meshes per material, while retaining vertex colors and vertex normals on the split boundary
- exports multi-material meshes correctly
- exports LOD groups (LODed meshes to work with StormEngine2 and RBDoom 3 BFG)
- batch export (exports multiple meshes at one, including LODed meshes and split meshes)

io_export_ase - ASE Exporter for Blender 3.4.1

ASE258.py - Original ASE Exporter for Blender 2.76+
