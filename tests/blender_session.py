"""Deterministic real-Blender session lifecycle tests, no live mouse/GPU claim."""
from pathlib import Path
from types import SimpleNamespace
import sys
import bpy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'blender'))
import retopoflow_rs as addon
from retopoflow_rs import session

class ContextProxy:
    def __getattr__(self,name):
        if name=='window_manager':
            return SimpleNamespace(modal_handler_add=lambda operator:None)
        return getattr(bpy.context,name)

class Harness:
    def __init__(self,new_target=False):
        self.tool='Select'; self.new_target=new_target; self.reports=[]
    def report(self,kind,message): self.reports.append((kind,message))

# Keep real session implementation; bypass only RNA construction and WM's live
# event registration so tests can deterministically deliver events in background.
for name,value in session.RF_RS_OT_session.__dict__.items():
    if callable(value) or isinstance(value,staticmethod): setattr(Harness,name,value)


def main():
    addon.register()
    for obj in list(bpy.data.objects): bpy.data.objects.remove(obj,do_unlink=True)
    owned=[]
    def new(name,vertices,faces):
        mesh=bpy.data.meshes.new(name); mesh.from_pydata(vertices,[],faces)
        obj=bpy.data.objects.new(name,mesh); bpy.context.collection.objects.link(obj)
        owned.append(obj); return obj
    def active(obj):
        for other in bpy.context.selected_objects: other.select_set(False)
        obj.select_set(True); bpy.context.view_layer.objects.active=obj
        bpy.context.view_layer.update()
    def event(kind,value='PRESS',**kw):
        return SimpleNamespace(type=kind,value=value,mouse_x=500,mouse_y=350,
                               ctrl=kw.get('ctrl',False),shift=kw.get('shift',False),alt=False)
    area=next(a for a in bpy.context.screen.areas if a.type=='VIEW_3D')
    region=next(r for r in area.regions if r.type=='WINDOW')
    source=new('Session source',[(-10,-10,0),(10,-10,0),(10,10,0),(-10,10,0)],[(0,1,2,3)])
    source2=new('Second source',[(20,0,0),(22,0,0),(20,2,0)],[(0,1,2)])
    target=new('Session target',[],[])
    addon_source_before=bpy.context.scene.rf_rs_source
    ctx=ContextProxy()
    try:
        with bpy.context.temp_override(area=area,region=region):
            active(target)
            space=bpy.context.space_data
            saved=(space.show_gizmo,space.show_region_ui,space.show_region_toolbar,
                   target.display_type,target.show_in_front)
            bpy.ops.object.mode_set(mode='EDIT')
            s=Harness()
            assert s.invoke(ctx,event('NONE'))=={'RUNNING_MODAL'}, s.reports
            assert session.active_session() is s and bpy.context.mode=='OBJECT'
            assert len(s._source['triangles'])==3
            assert target.display_type=='WIRE' and target.show_in_front
            assert not space.show_gizmo and not space.show_region_ui, (space.show_gizmo,space.show_region_ui,[(name,value) for _,name,value in s._settings])
            s._mutate(ctx,'add_vertex',[(0,0,1)])
            s._mutate(ctx,'add_vertex',[(1,0,1)])
            assert len(target.data.vertices)==2 and len(s._history.undo_stack)==2
            try:
                s._mutate(ctx,'knife',extra={'normal':[0,0,0]})
                raise AssertionError('Invalid action succeeded')
            except RuntimeError:
                pass
            assert len(target.data.vertices)==2 and s._history.pending is None
            assert len(s._history.undo_stack)==2
            assert s._history.step() and len(target.data.vertices)==1
            assert s._history.step() and len(target.data.vertices)==0
            assert s._history.step(True) and len(target.data.vertices)==1
            assert s._history.step(True) and len(target.data.vertices)==2
            s._points=[(0,0,0)]; s._screen=[(10,10)]; s._drawing=True
            assert s.modal(ctx,event('ESC'))=={'RUNNING_MODAL'}
            assert session.active_session() is s and len(target.data.vertices)==2
            assert not s._points and not s._drawing
            # Match RF numeric switching inside the same operator.
            s._inside=lambda event:True
            s._ui=lambda event:False
            assert s.modal(ctx,event('FIVE'))=={'RUNNING_MODAL'}
            assert s.tool=='PolyPen' and len(target.data.vertices)==2
            released_handle=s._source_handle
            assert s.modal(ctx,event('TAB'))=={'FINISHED'}
            assert bpy.context.mode=='EDIT_MESH'
            assert session.active_session() is None and s._source_handle is None and not s._handles
            assert (space.show_gizmo,space.show_region_ui,space.show_region_toolbar,
                    target.display_type,target.show_in_front)==saved
            assert bpy.context.scene.rf_rs_source==addon_source_before
            try:
                addon._engine({'mesh':{'vertices':[]},'operation':'project'},released_handle)
                raise AssertionError('Session source handle leaked')
            except RuntimeError as error:
                assert 'Unknown source handle' in str(error)
            bpy.ops.object.mode_set(mode='OBJECT')
            assert len(target.data.vertices)==2

            # Whole-session cancellation restores geometry, not just settings.
            s=Harness(); assert s.invoke(ctx,event('NONE'))=={'RUNNING_MODAL'},s.reports
            s._mutate(ctx,'add_vertex',[(2,0,1)])
            assert len(target.data.vertices)==3
            assert s.modal(ctx,event('ESC',shift=True))=={'CANCELLED'}
            assert len(target.data.vertices)==2 and session.active_session() is None

            # Open boundary matching uses native count = vertices, not cells.
            chain=new('Chain',[(0,0,0),(1,0,0),(2,0,0)],[])
            chain.data.edges.add(2)
            chain.data.edges[0].vertices=(0,1); chain.data.edges[1].vertices=(1,2)
            active(chain); addon._set_selection(chain.data,[0,1,2])
            bpy.context.scene.rf_rs_count=3
            s=Harness(); assert s.invoke(ctx,event('NONE'))=={'RUNNING_MODAL'},s.reports
            assert s._boundary(False)
            s._finish(ctx)

            # New target normal exit keeps retopology active in Edit Mode.
            active(source)
            s=Harness(True); assert s.invoke(ctx,event('NONE'))=={'RUNNING_MODAL'},s.reports
            created=s._target; owned.append(created)
            s._mutate(ctx,'add_vertex',[(0,0,1)])
            s._finish(ctx)
            assert bpy.context.active_object==created and bpy.context.mode=='EDIT_MESH'
            bpy.ops.object.mode_set(mode='OBJECT')
            active(source)
            s=Harness(True); assert s.invoke(ctx,event('NONE'))=={'RUNNING_MODAL'},s.reports
            created_name=s._target.name
            s._finish(ctx,True)
            assert created_name not in bpy.data.objects and bpy.context.active_object==source

            # Partial startup error must restore mode/UI and clear singleton.
            active(target); source.hide_set(True); source2.hide_set(True); chain.hide_set(True)
            for obj in owned:
                if obj not in (target,source,source2,chain): obj.hide_set(True)
            bpy.ops.object.mode_set(mode='EDIT')
            s=Harness(); assert s.invoke(ctx,event('NONE'))=={'CANCELLED'}
            assert bpy.context.mode=='EDIT_MESH' and session.active_session() is None
            assert (space.show_gizmo,space.show_region_ui,space.show_region_toolbar,
                    target.display_type,target.show_in_front)==saved
            bpy.ops.object.mode_set(mode='OBJECT')
        print('Rust Blender viewport session lifecycle tests passed')
    finally:
        if session.active_session(): session.shutdown(bpy.context)
        if bpy.context.mode=='EDIT_MESH': bpy.ops.object.mode_set(mode='OBJECT')
        for obj in owned:
            try:
                mesh=obj.data; bpy.data.objects.remove(obj,do_unlink=True)
                if mesh.users==0: bpy.data.meshes.remove(mesh)
            except ReferenceError: pass
        addon.unregister()

if __name__=='__main__': main()
