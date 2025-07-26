import sys
from PIL import Image, ImageChops
import VTFLibWrapper.VTFLib as VTFLib
import VTFLibWrapper.VTFLibEnums as VTFLibEnums

def convert_image(input_png: str, output_vtf: str, clamp: int):
    img = Image.open(input_png).convert("RGBA")

    w, h = img.size
    if clamp > 0 and max(w, h) > clamp:
        scale = clamp / float(max(w, h))
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        w, h = img.size

    data = img.tobytes()
    a_min, a_max = img.getchannel("A").getextrema()
    fmt = (VTFLibEnums.ImageFormat.ImageFormatDXT1
           if a_min == 255 and a_max == 255
           else VTFLibEnums.ImageFormat.ImageFormatDXT5)

    vtf_lib = VTFLib.VTFLib()
    opts = vtf_lib.create_default_params_structure()
    opts.ImageFormat = fmt
    opts.Flags       = VTFLibEnums.ImageFlag.ImageFlagEightBitAlpha
    opts.Resize      = 1

    vtf_lib.image_create_single(w, h, data, opts)
    vtf_lib.image_save(output_vtf)

def main():
    args = sys.argv[1:]
    if len(args) != 3:
        print("Usage: convert_image.py <in.png> <out.vtf> <clamp>", file=sys.stderr)
        sys.exit(1)
    in_png, out_vtf, clamp = args[0], args[1], int(args[2])
    try:
        convert_image(in_png, out_vtf, clamp)
    except Exception as e:
        print(f"Conversion failed: {e}", file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()
