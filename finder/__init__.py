"""Shopify storefront discovery."""

# Single source of truth for the version. build_installer.bat reads this and
# passes it to both PyInstaller and Inno Setup, so a release cannot end up with
# the exe saying one thing and Add/Remove Programs saying another.
__version__ = "1.0.1"
