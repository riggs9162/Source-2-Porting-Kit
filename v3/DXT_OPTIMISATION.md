# DXT alpha storage optimisation

The shared VTF encoder and resize pipeline use the alpha-fitting strategy from D:/Github/fOptimizer/src/foptimizer/backend/tools/image_conversion.py (fit_dxt). DXT3/DXT5 with opaque alpha can become DXT1; binary alpha can become DXT1_ONE_BIT_ALPHA. Graded or entirely zero alpha retains its format. Uncompressed textures, including flow maps, retain their format.

The porter checks every mip, frame, face and depth slice. It additionally compares every decoded RGBA pixel before accepting a conversion, including opaque conversions. Binary transparency that would crush RGB in a packed shader mask therefore retains its original storage. Dimensions and mip counts are preserved, normal flags are retained, and alpha flags follow the resulting storage. No existing addon files are modified by this pipeline change.

Validation: python -m pytest v3/tests/test_dxt_optimisation.py v3/tests/test_materials_glass.py -q (9 passed), plus Python compilation of textures.py.
