"""Headless Rust/Blender operation integration, including rejection atomicity.
Run after build_addon.py using --python-exit-code 1.
"""
from pathlib import Path
import sys
import bpy
from mathutils import Vector
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'blender'))
import retopoflow_rs as addon


def main():
    addon.register()
    objects = []
    def new(name, vertices=(), edges=(), faces=()):
        mesh=bpy.data.meshes.new(name)
        mesh.from_pydata(vertices,edges,faces)
        obj=bpy.data.objects.new(name,mesh)
        bpy.context.collection.objects.link(obj)
        objects.append(obj)
        for other in bpy.context.selected_objects: other.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active=obj
        return obj
    def select(obj,indices):
        for v in obj.data.vertices: v.select=v.index in indices
    def apply(op, points=(), **extra):
        addon._execute(bpy.context,op,points,extra=extra)
    def square(name):
        return new(name,[(0,0,0),(2,0,0),(2,2,0),(0,2,0)],[],[(0,1,2,3)])
    try:
        source=new('Tools source',[(-10,-10,0),(10,-10,0),(10,10,0),(-10,10,0)],[],[(0,1,2,3)])
        bpy.context.scene.rf_rs_source=source
        assert bpy.ops.mesh.rf_rs_new_target()=={'FINISHED'}
        created=bpy.context.active_object
        objects.append(created)
        assert created != source and created.type=='MESH'
        assert created.show_in_front and created.display_type=='WIRE'
        assert len(created.data.vertices)==0
        cached=addon._cache_source(addon._source_geometry(bpy.context,source))
        try:
            addon._execute(bpy.context,'add_vertex',[(0,0,1)],source_handle=cached)
            addon._execute(bpy.context,'add_vertex',[(1,0,1)],source_handle=cached)
            assert len(created.data.vertices)==2
            assert all(abs(v.co.z)<1e-6 for v in created.data.vertices)
        finally:
            addon._load_library().rf_source_free(cached)
        target=square('Loops')
        apply('loops',edge=[0,1],factor=0.25)
        assert len(target.data.polygons)==2
        assert len(target.data.vertices)==6
        apply('knife',origin=[0,1,0],normal=[0,1,0])
        assert len(target.data.polygons)==4
        assert not target.data.validate(verbose=True)

        target=new('Patch')
        apply('patch',[(0,0,1),(2,0,1),(2,2,1),(0,2,1)],count=2,rows=2)
        assert len(target.data.polygons)==4 and len(target.data.vertices)==9
        select(target,range(9)); apply('select_none')
        assert not any(v.select for v in target.data.vertices)
        apply('select_all'); assert all(v.select for v in target.data.vertices)
        uv=target.data.uv_layers.new(name='Preserved UV')
        uv.data[0].uv=(0.25,0.75)
        original=target.data
        apply('tweak',[(0.1,0.1,2)])
        assert target.data==original
        assert (Vector(target.data.uv_layers[0].data[0].uv)-Vector((0.25,0.75))).length<1e-6
        try:
            apply('loops',edge=[0,1],factor=0.5)
            raise AssertionError('Attributed topology edit unexpectedly succeeded')
        except RuntimeError as error:
            assert 'Topology edits require' in str(error)
        assert target.data==original and len(target.data.vertices)==9

        target=new('Strip')
        apply('polystrip',[(0,0,1),(3,0,1)],count=4,width=0.5)
        assert len(target.data.polygons)==3 and len(target.data.vertices)==8
        assert not target.data.validate(verbose=True)

        target=new('Bridge',[(0,0,0),(1,0,0),(2,0,0)],[(0,1),(1,2)])
        select(target,{0,1,2})
        apply('bridge',[(0,1,0),(2,1,0)],count=3)
        assert len(target.data.polygons)==2

        target=new('Pen',[(0,0,0),(1,0,0)],[(0,1)])
        select(target,{0,1})
        apply('polypen',[(0.5,1,1)],width=0.01)
        assert len(target.data.polygons)==1
        assert addon._selected(target)==[1,2], addon._selected(target)
        apply('polypen',[(1.5,1,0)],width=0.01)
        assert len(target.data.polygons)==2
        assert addon._selected(target)==[2,3], addon._selected(target)
        # Isolate edge split and merge from the consecutive insertion check.
        target=new('Pen split',[(0,0,0),(1,0,0),(0.5,1,0)],[],[(0,1,2)])
        apply('polypen',[(0.5,0,0)],edge=[0,1],width=0.01)
        assert len(target.data.vertices)==4 and len(target.data.polygons[0].vertices)==4
        select(target,{0,3}); apply('merge')
        assert len(target.data.vertices)==3

        vertices=[(0,0,0),(1,0,0),(2,0,0),(2,1,0),(2,2,0),(1,2,0),(0,2,0),(0,1,0)]
        target=new('Boundary fill',vertices,[(i,(i+1)%8) for i in range(8)])
        select(target,set(range(8))); apply('fill_patch',count=2,rows=2)
        assert len(target.data.polygons)==4 and len(target.data.vertices)==9

        # Open plane section must remain open, rather than guessed as a loop.
        target=new('Contour')
        apply('contour',[(0,0,0)],origin=[0,0,0],normal=[1,0,0],count=5)
        assert len(target.data.vertices)==5 and len(target.data.edges)==4
        old=target.data
        try:
            apply('knife',origin=[0,0,0],normal=[0,0,0])
            raise AssertionError('Invalid knife succeeded')
        except RuntimeError:
            pass
        assert target.data==old
        print('Rust Blender tool integration tests passed')
    finally:
        for obj in objects:
            mesh=obj.data
            bpy.data.objects.remove(obj,do_unlink=True)
            if mesh.users==0: bpy.data.meshes.remove(mesh)
        addon.unregister()

if __name__=='__main__': main()
