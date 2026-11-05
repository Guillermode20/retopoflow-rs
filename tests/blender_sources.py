"""Regression tests for source startup and degenerate evaluated triangles."""
from pathlib import Path
import sys
import bpy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'blender'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from blender_session import Harness, ContextProxy
import retopoflow_rs as addon
from retopoflow_rs import session


def main():
    addon.register()
    for obj in list(bpy.data.objects): bpy.data.objects.remove(obj,do_unlink=True)
    def new(name,verts,faces):
        m=bpy.data.meshes.new(name); m.from_pydata(verts,[],faces)
        obj=bpy.data.objects.new(name,m); bpy.context.collection.objects.link(obj); return obj
    def active(obj):
        for other in bpy.context.selected_objects: other.select_set(False)
        obj.select_set(True); bpy.context.view_layer.objects.active=obj; bpy.context.view_layer.update()
    area=next(a for a in bpy.context.screen.areas if a.type=='VIEW_3D')
    region=next(r for r in area.regions if r.type=='WINDOW')
    try:
        source=new('Cube_requad',[(0,0,0),(1,0,0),(0,1,0),(2,0,0)],[(0,1,2),(0,1,3)])
        original=len(source.data.vertices)
        data=addon._source_geometry(bpy.context,source)
        assert len(data['triangles'])==1 and data['skipped_triangles']==1
        handle=addon._cache_source(data); addon._load_library().rf_source_free(handle)
        with bpy.context.temp_override(area=area,region=region):
            # A lone source launched through the main header creates its target.
            active(source)
            s=Harness(); assert s.invoke(ContextProxy(),None)=={'RUNNING_MODAL'},s.reports
            assert s._target!=source and len(source.data.vertices)==original
            assert s._sources==(source,)
            s._finish(ContextProxy(),True)
            assert bpy.context.active_object==source
            # Explicit source picker wins even when that source is active.
            bpy.context.scene.rf_rs_source=source
            s=Harness(); assert s.invoke(ContextProxy(),None)=={'RUNNING_MODAL'},s.reports
            assert s._target!=source
            s._finish(ContextProxy(),True)
            # Explicit sources can be hidden; target remains a separate object.
            target=new('Low-poly target',[],[]); active(target); source.hide_set(True)
            s=Harness(); assert s.invoke(ContextProxy(),None)=={'RUNNING_MODAL'},s.reports
            assert s._target==target and source in s._sources
            s._finish(ContextProxy())
            assert source.hide_get()
        tiny=new('Tiny source',[(0,0,0),(1e-9,0,0),(0,1e-9,0)],[(0,1,2)])
        data=addon._source_geometry(bpy.context,tiny)
        assert len(data['triangles'])==1
        handle=addon._cache_source(data); addon._load_library().rf_source_free(handle)
        broken=new('Collapsed source',[(0,0,0),(1,0,0),(2,0,0)],[(0,1,2)])
        try:
            addon._source_geometry(bpy.context,broken)
            raise AssertionError('Collapsed source accepted')
        except RuntimeError as error:
            assert 'Collapsed source' in str(error) and 'zero-area' in str(error)
        try:
            addon._cache_source({'vertices':[[0,0,0]],'triangles':[[0,1,2]]})
            raise AssertionError('Invalid source accepted')
        except RuntimeError as error:
            assert 'index out of bounds' in str(error),str(error)
        assert len(source.data.polygons)==2  # Cleanup never edits source meshes.
        print('Rust Blender source startup regression tests passed')
    finally:
        session.shutdown(bpy.context)
        if bpy.context.mode=='EDIT_MESH': bpy.ops.object.mode_set(mode='OBJECT')
        for obj in list(bpy.data.objects): bpy.data.objects.remove(obj,do_unlink=True)
        addon.unregister()

if __name__=='__main__': main()
