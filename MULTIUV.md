# MultiUV: one UV map per material (io_export_idt4ase)

## What the engine can do

idTech 4 stores exactly one texture coordinate per vertex. There are no UV
channels a material could switch between: the ASE loader reads the primary
`MESH_TVERTLIST` only and ignores `MESH_MAPPINGCHANNEL` blocks. What the
engine does allow is different UV maps on different polygons of one model,
because every material becomes its own GEOMOBJECT with its own texture
coordinates. That is how in-game GUI screens work: the screen polygons are
a separate surface with a map laid out in 0..1, the body is another surface
with another map.

Blender UV maps are dense (every corner has a value in every map), so the
exporter has to decide which map each GEOMOBJECT carries. MultiUV automates
that decision.

## Setup in Blender

1. Create the UV maps you need on the mesh, for example `body` and `screen`,
   and unwrap each set of faces in its own map, each in 0..1. There is no
   need to move the other layout out of the way: maps of different
   materials never meet in the engine.
2. In each material's node tree add a **UV Map** node set to the map that
   material should sample and wire it into the **Vector** input of the
   material's Image Texture. This is the same node Blender needs to preview
   the right map in the viewport, so you are not adding anything extra.
3. Assign faces to materials as usual.
4. Export with **MultiUV (per-material UV maps)** ticked.

Materials are shared between objects and the node names the map, so a
material used on several objects works as long as each object has a UV map
of that name.

## What the exporter does

Each GEOMOBJECT has one material. With MultiUV on, its primary UV channel
(`MESH_TVERTLIST` / `MESH_TFACELIST`) is written from the map named by that
material's UV Map node (first the one wired into an Image Texture, otherwise
any UV Map node in the tree). Additional mapping channels are still written
exactly as before; the engine ignores them.

Fallbacks, with a message in the Blender console:

- Material without a UV Map node: the mesh's active UV map, which is what
  the exporter uses for everything when MultiUV is off.
- Material naming a map the mesh does not have: the active UV map.

With MikkT enabled, tangents are computed per map too, so each corner's
tangent matches the map its material samples. A material with no UV map at
all gets no tangents and the engine derives them for that surface.

With MultiUV off the output is identical to earlier versions.

## The terminal example

One mesh, faces split between `models/terminal/body` and a GUI material.
UV maps `body` and `screen`. Body material: UV Map node `body` into its
Image Texture. GUI material: UV Map node `screen` into its Image Texture
(the texture only serves the preview). Export with MultiUV on. The body
GEOMOBJECT carries the `body` coordinates, the screen GEOMOBJECT the
`screen` coordinates, each clean in 0..1.
