"""Kaggle Render - render Blender scenes on Kaggle GPUs (one account, or split across several)."""


def register():
    from . import ui  # imported here so the rest of the package (runner, kaggle_cli) works without bpy
    ui.register()


def unregister():
    from . import ui
    ui.unregister()
