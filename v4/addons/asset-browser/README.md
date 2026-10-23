# Asset browser

Provides a native, paged browser plus `asset-browser/list`. Folder and VPK
providers expose small metadata records and on-demand streams through the common
`IAssetProvider` contract. The browser displays 100 rows at a time and can transfer
a selected asset to the VRF export form when the VRF add-on is enabled.

VPK v1/v2 archives may use embedded data, preload bytes, or numbered archive
parts. Archive reads do not extract or decode content. Nested archives and archive
CRC/signature verification are not supported. The index is scanned as needed;
the provider keeps no whole-game metadata cache.

This is the asset-selection workspace, not the future map/model preview renderer.
