import os
import re
import sys
import tempfile
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from PIL import Image, ImageTk, ImageChops

# Wrap VTFLib imports to avoid startup crash if unavailable
try:
    import VTFLibWrapper.VTFLib as VTFLib
    import VTFLibWrapper.VTFLibEnums as VTFLibEnums
    VTFLIB_AVAILABLE = True
except ImportError:
    VTFLIB_AVAILABLE = False

class VmatToVmtApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("VMAT → VMT/VTF with Baking & Metalness")
        self.geometry("800x750")
        self.base_image = None
        self.baked_image = None
        self.rough_image = None
        self.ao_image = None
        self._build_ui()

    def _build_ui(self):
        pad = {"padx":5, "pady":5}
        ttk.Label(self, text="VMAT File:").grid(row=0, column=0, sticky="e", **pad)
        self.vmat_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.vmat_var, width=60).grid(row=0, column=1, **pad)
        ttk.Button(self, text="Browse…", command=self._browse_vmat).grid(row=0, column=2, **pad)

        ttk.Label(self, text="Output Folder:").grid(row=1, column=0, sticky="e", **pad)
        self.out_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.out_var, width=60).grid(row=1, column=1, **pad)
        ttk.Button(self, text="Browse…", command=self._browse_out).grid(row=1, column=2, **pad)

        ttk.Label(self, text="Material Prefix:").grid(row=2, column=0, sticky="e", **pad)
        self.mat_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.mat_var, width=60).grid(row=2, column=1, columnspan=2, **pad)

        ttk.Label(self, text="Clamp Size (px, 0=no clamp):").grid(row=3, column=0, sticky="e", **pad)
        self.clamp_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.clamp_var, width=10).grid(row=3, column=1, sticky="w", **pad)

        self.bake_rough_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text="Bake Roughness", variable=self.bake_rough_var,
                        command=self.update_preview).grid(row=4, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(self, text="Roughness Strength (%)").grid(row=5, column=0, sticky="e", **pad)
        self.rough_slider = tk.Scale(self, from_=0, to=100, orient=tk.HORIZONTAL,
                                     length=300, command=lambda e: self.update_preview())
        self.rough_slider.set(100)
        self.rough_slider.grid(row=5, column=1, sticky="w", **pad)

        self.bake_ao_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text="Bake AO", variable=self.bake_ao_var,
                        command=self.update_preview).grid(row=6, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(self, text="AO Strength (%)").grid(row=7, column=0, sticky="e", **pad)
        self.ao_slider = tk.Scale(self, from_=0, to=100, orient=tk.HORIZONTAL,
                                  length=300, command=lambda e: self.update_preview())
        self.ao_slider.set(100)
        self.ao_slider.grid(row=7, column=1, sticky="w", **pad)

        # Preview
        preview = ttk.LabelFrame(self, text="Preview (Left: Base | Right: Baked)")
        preview.grid(row=8, column=0, columnspan=3, sticky="nsew", padx=10, pady=10)
        self.preview_base = tk.Label(preview); self.preview_base.pack(side="left", padx=10, pady=10)
        self.preview_baked = tk.Label(preview); self.preview_baked.pack(side="right", padx=10, pady=10)
        self.grid_rowconfigure(8, weight=1); self.grid_columnconfigure(1, weight=1)

        ttk.Button(self, text="Convert with Baking", command=self._on_convert).grid(
            row=9, column=0, columnspan=3, pady=(10,5)
        )

        self.log = ScrolledText(self, height=10, wrap="word")
        self.log.grid(row=10, column=0, columnspan=3, sticky="nsew", padx=5, pady=5)
        self.grid_rowconfigure(10, weight=1)

    def _browse_vmat(self):
        path = filedialog.askopenfilename(title="Select .vmat", filetypes=[("VMAT","*.vmat"),("All","*.*")])
        if path:
            self.vmat_var.set(path)
            self.out_var.set(os.path.dirname(path))
            self._load_aux_textures()

    def _browse_out(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d: self.out_var.set(d)

    def _log(self,msg):
        self.log.insert(tk.END, msg+"\n"); self.log.see(tk.END)

    def _parse_vmat(self):
        path=self.vmat_var.get().strip()
        if not path or not os.path.isfile(path): return None, {}
        shader,props="VertexLitGeneric",{}
        in_block=False
        with open(path,"r",encoding="utf-8",errors="ignore") as f:
            for ln in f:
                m0=re.match(r'^\s*"([^"]+)"',ln)
                if m0 and not in_block: shader=m0.group(1)
                s=ln.strip()
                if s=="{": in_block=True; continue
                if s=="}": in_block=False; continue
                if in_block:
                    m=re.match(r'^\s*"([^"]+)"\s*"([^"]+)"',ln)
                    if m: props[m.group(1)]=m.group(2)
        if shader.lower()=="layer0": shader="VertexLitGeneric"
        return shader,props

    def _find_texture_file(self, base_folder, texture_path):
        """Find a texture file, checking both the base folder and subfolders."""
        if not texture_path:
            return None
        
        # First try the exact basename in the base folder
        basename = os.path.basename(texture_path)
        direct_path = os.path.join(base_folder, basename)
        if os.path.isfile(direct_path):
            return direct_path
        
        # If not found, search in subfolders
        for root, dirs, files in os.walk(base_folder):
            if basename in files:
                found_path = os.path.join(root, basename)
                if os.path.isfile(found_path):
                    self._log(f"Found texture in subfolder: {found_path}")
                    return found_path
        
        self._log(f"Warning: Could not find texture file: {basename}")
        return None

    def _load_aux_textures(self):
        shader,props=self._parse_vmat()
        if not props: return
        fld=os.path.dirname(self.vmat_var.get())
        
        # Load roughness texture
        rkey=props.get("TextureRoughness")
        if rkey:
            p = self._find_texture_file(fld, rkey)
            if p: self.rough_image=Image.open(p).convert("L")
        
        # Load ambient occlusion texture
        aok=props.get("TextureAmbientOcclusion")
        if aok:
            p = self._find_texture_file(fld, aok)
            if p: self.ao_image=Image.open(p).convert("L")
        
        self.update_preview()

    def update_preview(self,event=None):
        path=self.vmat_var.get().strip()
        if not path or not os.path.isfile(path): return
        shader,props=self._parse_vmat()
        base=props.get("TextureColor")
        if not base: return
        fld=os.path.dirname(path)
        png = self._find_texture_file(fld, base)
        if not png: return
        self.base_image=Image.open(png).convert("RGB")
        img=self.base_image.copy()
        if self.bake_rough_var.get() and self.rough_image:
            rough=self.rough_image.resize(img.size)
            img=Image.blend(img,ImageChops.multiply(img,Image.merge("RGB",[rough]*3)),self.rough_slider.get()/100)
        if self.bake_ao_var.get() and self.ao_image:
            ao=self.ao_image.resize(img.size)
            img=Image.blend(img,ImageChops.multiply(img,Image.merge("RGB",[ao]*3)),self.ao_slider.get()/100)
        self.baked_image=img
        disp_b=self.base_image.resize((256,256),Image.LANCZOS)
        disp_k=img.resize((256,256),Image.LANCZOS)
        self.base_tk=ImageTk.PhotoImage(disp_b); self.baked_tk=ImageTk.PhotoImage(disp_k)
        self.preview_base.config(image=self.base_tk); self.preview_baked.config(image=self.baked_tk)

    def _on_convert(self):
        if not VTFLIB_AVAILABLE:
            messagebox.showerror("Error","VTFLibWrapper not available."); return
        shader,props=self._parse_vmat()
        vmat=self.vmat_var.get().strip()
        if not props.get("TextureColor"):
            messagebox.showerror("Error","VMAT missing TextureColor."); return
        out=self.out_var.get().strip() or os.path.dirname(vmat)
        prefix=self.mat_var.get().strip()
        try: clamp=int(self.clamp_var.get().strip())
        except: messagebox.showerror("Error","Clamp must be integer."); return
        if prefix and not prefix.endswith("/"): prefix+="/"
        self.log.delete("1.0","end")
        count=0

        def run_convert(src,dst):
            cmd=[sys.executable,os.path.join(os.path.dirname(__file__),"convert_image.py"),src,dst,str(clamp)]
            proc=subprocess.run(cmd,capture_output=True,text=True)
            if proc.returncode!=0:
                self._log(f"[ERROR] {os.path.basename(dst)} → VTF failed:\n{proc.stderr.strip()}")
                return False
            self._log(f"[OK] {os.path.basename(dst)}")
            return True

        # Base
        base_key=props["TextureColor"]
        base_name=os.path.splitext(os.path.basename(vmat))[0]  # Use VMAT name instead of color texture name
        base_dst=os.path.join(out,base_name+".vtf")
        if self.baked_image:
            tmp=tempfile.NamedTemporaryFile(suffix=".png",delete=False)
            self.baked_image.save(tmp.name); ok=run_convert(tmp.name,base_dst); tmp.close(); os.unlink(tmp.name)
        else:
            base_png = self._find_texture_file(os.path.dirname(vmat), base_key)
            if base_png:
                ok=run_convert(base_png, base_dst)
            else:
                self._log(f"[ERROR] Could not find base texture: {base_key}")
                ok=False
        if ok: count+=1

        # Normal/Metalness
        bump=props.get("TextureNormal"); metal=props.get("TextureMetalness")
        norm_src=None
        if metal:
            # merge metal into alpha then invert immediately
            if bump:
                bump_file = self._find_texture_file(os.path.dirname(vmat), bump)
                if bump_file:
                    nimg=Image.open(bump_file).convert("RGBA")
                else:
                    nimg=Image.new("RGBA",self.baked_image.size if self.baked_image else (512,512))
            else:
                nimg=Image.new("RGBA",self.baked_image.size if self.baked_image else (512,512))
            metal_file = self._find_texture_file(os.path.dirname(vmat), metal)
            if metal_file:
                mimg=Image.open(metal_file).convert("L")
                mimg=mimg.resize(nimg.size,Image.NEAREST)
                r,g,b,_=nimg.split(); nimg=Image.merge("RGBA",(r,g,b,mimg))
                # invert alpha channel now
                r2,g2,b2,a2=nimg.split(); a2=ImageChops.invert(a2); nimg=Image.merge("RGBA",(r2,g2,b2,a2))
                # save permanent PNG for inspection
                norm_png=os.path.join(out,base_name+"_normal.png")
                nimg.save(norm_png)
                norm_src=norm_png
        elif bump:
            norm_src = self._find_texture_file(os.path.dirname(vmat), bump)

        if norm_src:
            norm_dst=os.path.join(out,base_name+"_normal.vtf")
            if run_convert(norm_src,norm_dst):
                count+=1

        # Write VMT
        vmt_p=os.path.join(out,base_name+".vmt")
        try:
            with open(vmt_p,"w",encoding="utf-8") as v:
                v.write(f'"{shader}"\n{{\n')
                v.write(f'    "$basetexture" "{prefix}{base_name}"\n')
                if bump or metal:
                    v.write(f'    "$bumpmap" "{prefix}{base_name}_normal"\n')
                if metal:
                    v.write('    "$normalmapalphaenvmapmask" "1"\n\n')
                v.write('    "$envmap" "env_cubemap"\n')
                v.write('    "$envmaptint" "[0.5 0.5 0.5]"\n')
                v.write("}\n")
            self._log(f"[OK] Wrote VMT: {vmt_p}")
        except Exception as e:
            self._log(f"[ERROR] VMT write: {e}")

        messagebox.showinfo("Done", f"Converted {count} textures and wrote VMT.")

if __name__=="__main__":
    VmatToVmtApp().mainloop()
