"""After building: blender --background --factory-startup --python-exit-code 1 --python tests/blender_smoke.py"""
import sys
from pathlib import Path

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'blender'))
import retopoflow_rs


def main():
    retopoflow_rs.register()
    objects = []
    try:
        source_mesh = bpy.data.meshes.new('RS smoke source')
        source_mesh.from_pydata([(-10, -10, 0), (10, -10, 0), (10, 10, 0), (-10, 10, 0)], [], [(0, 1, 2, 3)])
        source = bpy.data.objects.new('RS smoke source', source_mesh)
        bpy.context.collection.objects.link(source)
        objects.append(source)
        mesh = bpy.data.meshes.new('RS smoke target')
        mesh.from_pydata([(0, 0, 1), (1, 0, 1), (0, 1, 1)], [], [(0, 1, 2)])
        target = bpy.data.objects.new('RS smoke target', mesh)
        bpy.context.collection.objects.link(target)
        objects.append(target)
        # Exercise conversion between world-space engine geometry and target local.
        target.location = (1, 2, 3)
        for vertex in mesh.vertices:
            vertex.select = True
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        target.select_set(True)
        bpy.context.view_layer.objects.active = target
        bpy.context.scene.rf_rs_source = source
        bpy.context.view_layer.update()
        result = bpy.ops.mesh.rf_rs_execute(operation='project')
        assert result == {'FINISHED'}, result
        assert len(target.data.vertices) == 3
        for vertex in target.data.vertices:
            world = target.matrix_world @ Vector(vertex.co)
            assert abs(world.z) < 1e-5, tuple(world)
        print('Rust Blender project smoke test passed')
    finally:
        for obj in objects:
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        retopoflow_rs.unregister()
    assert not hasattr(bpy.types.Scene, 'rf_rs_source')


if __name__ == '__main__':
    main()
