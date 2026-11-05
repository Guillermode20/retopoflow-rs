"""Retopoflow RS: Rust-backed surface retopology for Blender.

Module map:
  engine.py   ctypes bridge to the native library + mesh I/O (numpy)
  target.py   cached target topology, projection and occlusion-aware picking
  hud.py      viewport HUD layout (pure) and drawing
  gfx.py      GPU/BLF helpers
  session.py  the modal retopology session (primary UI)
  legacy.py   standalone one-shot operators for scripting
"""
bl_info = {
    'name': 'Retopoflow RS',
    'author': 'Retopoflow RS contributors',
    'version': (0, 3, 0),
    'blender': (4, 2, 0),
    'location': 'View3D > Header > Retopo RS  ·  Sidebar > Retopo RS',
    'description': 'Rust-backed surface retopology tools in one viewport session',
    'category': 'Mesh',
}

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty

from . import engine, legacy, session
from .hud import TOOLS


def _source_poll(self, obj):
    return obj.type == 'MESH' and not getattr(obj, 'rf_rs_is_target', False)


def _target_poll(self, obj):
    return obj.type == 'MESH'


class RF_RS_PT_main(bpy.types.Panel):
    bl_label = 'Retopo RS'
    bl_idname = 'RF_RS_PT_main'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Retopo RS'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.operator_context = 'INVOKE_DEFAULT'
        active = session.active_session()
        col = layout.column(align=True)
        col.scale_y = 1.4
        if active is not None:
            col.label(text='Session running in the viewport', icon='REC')
        else:
            col.operator('mesh.rf_rs_session', text='Start Retopology', icon='MOD_SHRINKWRAP')
            col.operator('mesh.rf_rs_session', text='New Target', icon='ADD').new_target = True
        layout.separator()
        col = layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(scene, 'rf_rs_source', text='Source')
        col.prop(scene, 'rf_rs_target', text='Target')
        col.prop(scene, 'rf_rs_tool', text='Start Tool')
        if scene.rf_rs_source is None:
            layout.label(text='Source: every visible mesh', icon='INFO')


class RF_RS_PT_settings(bpy.types.Panel):
    bl_label = 'Tool Settings'
    bl_idname = 'RF_RS_PT_settings'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Retopo RS'
    bl_parent_id = 'RF_RS_PT_main'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        scene = context.scene
        col = layout.column(align=True)
        col.prop(scene, 'rf_rs_count')
        col.prop(scene, 'rf_rs_rows')
        col.prop(scene, 'rf_rs_closed')
        col = layout.column(align=True)
        col.prop(scene, 'rf_rs_strip_px')
        col.prop(scene, 'rf_rs_pen_quads')
        col.prop(scene, 'rf_rs_loop_center')
        col = layout.column(align=True)
        col.prop(scene, 'rf_rs_brush_radius')
        col.prop(scene, 'rf_rs_strength')
        col.prop(scene, 'rf_rs_brush_selected')


class RF_RS_PT_legacy(bpy.types.Panel):
    bl_label = 'Standalone Commands'
    bl_idname = 'RF_RS_PT_legacy'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Retopo RS'
    bl_parent_id = 'RF_RS_PT_main'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        self.layout.operator_context = 'INVOKE_DEFAULT'
        self.layout.label(text='Target: active mesh, Object Mode', icon='INFO')
        legacy.draw_panel(self.layout, context)


def _header_session(self, context):
    if context.mode not in {'OBJECT', 'EDIT_MESH'}:
        return
    row = self.layout.row(align=True)
    row.operator_context = 'INVOKE_DEFAULT'
    row.operator('mesh.rf_rs_session', text='Retopo RS', icon='MOD_SHRINKWRAP')
    row.operator('mesh.rf_rs_session', text='', icon='ADD').new_target = True


_CLASSES = (*legacy.CLASSES, *session.CLASSES, RF_RS_PT_main, RF_RS_PT_settings, RF_RS_PT_legacy)
_SCENE_PROPERTIES = {
    'rf_rs_source': lambda: PointerProperty(
        name='Source', type=bpy.types.Object, poll=_source_poll,
        description='High-poly surface to retopologize. Empty: every visible mesh'),
    'rf_rs_target': lambda: PointerProperty(
        name='Target', type=bpy.types.Object, poll=_target_poll,
        description='Retopology mesh continued when the session starts from the source'),
    'rf_rs_tool': lambda: EnumProperty(
        name='Tool', items=[(t, t, hud_description(t)) for t in TOOLS], default='PolyPen',
        description='Tool active when a session starts (remembers the last one used)'),
    'rf_rs_strength': lambda: FloatProperty(name='Strength', default=0.5, min=0.0, max=1.0,
                                            description='Relax brush strength'),
    'rf_rs_iterations': lambda: IntProperty(name='Iterations', default=1, min=1, max=100),
    'rf_rs_count': lambda: IntProperty(name='Segments', default=8, min=2, max=4096,
                                       description='Vertices per stroke/contour; patch columns'),
    'rf_rs_rows': lambda: IntProperty(name='Rows', default=4, min=1, max=256),
    'rf_rs_width': lambda: FloatProperty(name='Width', default=0.1, min=0.000001,
                                         description='Standalone strip width in world units'),
    'rf_rs_factor': lambda: FloatProperty(name='Loop Factor', default=0.5, min=0.001, max=0.999),
    'rf_rs_closed': lambda: BoolProperty(name='Always Close Strokes', default=False),
    'rf_rs_strip_px': lambda: IntProperty(name='Strip Width (px)', default=40, min=4, max=400,
                                          description='PolyStrips width in screen pixels at the stroke start'),
    'rf_rs_brush_radius': lambda: IntProperty(name='Brush Radius (px)', default=60, min=5, max=500),
    'rf_rs_brush_selected': lambda: BoolProperty(name='Brush Selected Only', default=False),
    'rf_rs_pen_quads': lambda: BoolProperty(
        name='PolyPen Quads', default=True,
        description='A click after a triangle completes it to a quad, building strips'),
    'rf_rs_loop_center': lambda: BoolProperty(name='Loops at Center', default=False,
                                              description='Insert loops at edge midpoints'),
    'rf_rs_advanced': lambda: BoolProperty(name='Standalone commands', default=False),
}


def hud_description(tool):
    from .hud import DESCRIPTIONS
    return DESCRIPTIONS.get(tool, '')


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.rf_rs_is_target = BoolProperty(
        name='Retopo RS Target', default=False, options={'HIDDEN'},
        description='Created or used as a Retopo RS target; never treated as a source')
    for name, factory in _SCENE_PROPERTIES.items():
        setattr(bpy.types.Scene, name, factory())
    bpy.types.VIEW3D_HT_header.append(_header_session)


def unregister():
    try:
        session.shutdown(bpy.context)
        legacy.shutdown(bpy.context)
    finally:
        bpy.types.VIEW3D_HT_header.remove(_header_session)
        engine.forget_selection()
        for name in _SCENE_PROPERTIES:
            if hasattr(bpy.types.Scene, name):
                delattr(bpy.types.Scene, name)
        if hasattr(bpy.types.Object, 'rf_rs_is_target'):
            del bpy.types.Object.rf_rs_is_target
        for cls in reversed(_CLASSES):
            bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()
