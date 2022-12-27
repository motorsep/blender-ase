# ASE Export Script

This is a fork from the [original project](http://code.google.com/p/ase-export-vmc/) by MCampagnini. The script has been upgraded to support Blender 2.78a and Blender 2.80 and Blender 2.9 and Blender 3.4 (check the Releases).

## Videos
* Blender 2.63a and ASE 2.01 (http://www.youtube.com/watch?v=63Wz0FoK2U0)
* Blender 2.59 old version (http://www.youtube.com/watch?v=ZPQW5x1rhAY&feature=channel&list=UL)
* Blender 2.92 ASE exporter install (https://youtu.be/jv8xxFaK3t8)

## Installation
* Download and extract the ASE Export Script.
* In Blender, File (Edit in 2.9)/ User Preferences.
* Go to the Add-On Tab.
* Install Add-On, select the extracted python script.
* Click Save as Default (Save preferences or auto-save in Blender 2.9) to enable the script every time Blender loads (optional).

You will find a new export option in File / Export called Ascii Scene Export (.ase).  If you do not see the script in the export menu after following the installation instructions, open the Add-On tab and find the script under Import-Export: ASCII Scene Exporter.  Be sure that it is enabled by checking the box next to the add-on name.  Contact me on my website if you still have problems exporting.

## Mesh Requirements
* All non-collision geometries must be UV unwrapped.
* All non-collision meshes must have a material assigned in material slots and an image texture applied with Principled BSDF shader node only.
* All non-collision meshes must have only one uvmap per material.

## Optional
* Mesh may have more than one material
* Mesh may have more than one UV texture and/or a lightmap texture.
* Mesh may be assigned as a collision object.
* Mesh may have more than one collision object to determine collision borders.
* Mesh may be assigned smoothing groups.

## Collision Objects
Assign a mesh as a collision object by prefixing each object name with UCX_NAME, where NAME can be anything you would like (case-insensitive).

## Smoothing Groups
Assign smoothing groups by edge selecting the border of the faces you want assigned to a smoothing group, press ctrl+e and mark sharp.  Any face group separated with sharp edges will be assigned a smoothing group.

## Vertex Painting
Apply vertex painting in Blender as normal, be sure that you have at least two uv texture slots.  This is not a technical limitation, but due to time constraints I left the vertex painting code inside of a conditional that requires two uv textures.  In order to view your vertex painting in UDK you will have to import and set up your materials correctly within UDK.
## Issues
This version uses mesh duplication to avoid original meshes triangulation. Case it stops suddenly, as it happens during export errors, duplicates could appear in outliner window. They will show names like co#pia or UCX_co#pia as prefix and numbers like. 001, .002, 003 as suffix. Simply delete them.
This add-on didn't work with bad topology meshes. It fails often when importing 3d models from other blender versions or imported models. It's best to work them inside Blender 2.9 before exporting (cleanup meshes and assign new materials).

Common errors:
*TypeError: 'float' object cannot be interpreted as an integer
More than one UVmap assign per material. Remove UVmaps keep only one UVmap per material active.
*AttributeError: 'NoneType' object has no attribute 'name'
Image Texture is choosen but it was not loaded. Get the texture where you placed it.
*IndexError: tuple index out of range
Base color slot has no image texture assigned. Click on it and choose Image Texture on choice list.
*io_export_ase -RSmod.Error: Mesh must have at least one applied material.
Material need to be assign at least one per mesh.
*IndexError: bpy_prop_collection[index]: index 0 out of range, size 0
Material slot has one mesh without assigned material. Get a material for the mesh or meshes before trying to export it again.