"""Exercise modal state transitions with deterministic hits; no GPU/mouse claims."""
import sys
from pathlib import Path
from types import SimpleNamespace

import bpy
from mathutils import Vector

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'blender'))
import retopoflow_rs as addon


def main():
    addon.register()
    created=[]
    try:
        mesh=bpy.data.meshes.new('Modal source')
        mesh.from_pydata([(-10,-10,0),(10,-10,0),(0,10,0)],[],[(0,1,2)])
        source=bpy.data.objects.new('Modal source',mesh)
        bpy.context.collection.objects.link(source)
        created.append(source)
        bpy.context.scene.rf_rs_source=source
        assert bpy.ops.mesh.rf_rs_new_target()=={'FINISHED'}
        target=bpy.context.active_object
        created.append(target)
        area=next(a for a in bpy.context.screen.areas if a.type=='VIEW_3D')
        region=next(r for r in area.regions if r.type=='WINDOW')
        class Session:
            def __init__(self,operation,persistent=False):
                self.operation=operation; self.persistent=persistent
                self._area=area; self._target=target; self._points=[]; self._screen=[]
                self._drawing=False; self._commits=0; self.finished=False
                self._source=addon.engine.source_geometry(bpy.context,source)
                self._source_handle=addon.engine.cache_source(self._source)
                self.hit=Vector((0,0,0))
            def _inside(self,event): return True
            def _coordinate(self,event): return (10,10)
            def _hit(self,context,event): return self.hit.copy()
            def _edge(self,event): raise RuntimeError('No edge under deterministic hit')
            def _finish(self,context):
                if self._source_handle: addon.engine.free_source(self._source_handle)
                self._source_handle=None; self.finished=True
            def report(self,kind,message): self.last_error=message
        def event(kind,value='PRESS'):
            return SimpleNamespace(type=kind,value=value,alt=False)
        with bpy.context.temp_override(area=area,region=region):
            session=Session('polypen',True)
            try:
                assert addon.legacy.RF_RS_OT_draw.modal(session,bpy.context,event('LEFTMOUSE'))=={'RUNNING_MODAL'}
                session.hit=Vector((1,0,0))
                assert addon.legacy.RF_RS_OT_draw.modal(session,bpy.context,event('LEFTMOUSE'))=={'RUNNING_MODAL'}
                assert len(target.data.vertices)==2 and len(target.data.edges)==1
                assert addon.engine.selected(target)==[1]
                assert addon.legacy.RF_RS_OT_draw.modal(session,bpy.context,event('ESC'))=={'FINISHED'}
                assert session.finished and session._source_handle is None
            finally: session._finish(bpy.context)
            before=target.data
            session=Session('patch')
            try:
                for point in [(0,0,0),(1,0,0),(1,1,0)]:
                    session.hit=Vector(point)
                    assert addon.legacy.RF_RS_OT_draw.modal(session,bpy.context,event('LEFTMOUSE'))=={'RUNNING_MODAL'}
                assert addon.legacy.RF_RS_OT_draw.modal(session,bpy.context,event('RIGHTMOUSE'))=={'CANCELLED'}
                assert target.data==before and len(target.data.vertices)==2
            finally: session._finish(bpy.context)
        print('Rust Blender deterministic modal tests passed')
    finally:
        for obj in created:
            mesh=obj.data
            bpy.data.objects.remove(obj,do_unlink=True)
            if mesh.users==0: bpy.data.meshes.remove(mesh)
        addon.unregister()

if __name__=='__main__': main()
