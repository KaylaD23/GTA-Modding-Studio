"""PS2 RenderWare native texture property dialog, with guarded encoders.

Its controls mirror the texture editing *operations* in native TXD editors,
not their visual appearance. No selection silently modifies the source file.
"""
from __future__ import annotations
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

from PS2_TXD_Raster_Edit import (
    RASTER_MODES, PALETTE_CHOICES, PIXEL_FORMAT_CHOICES,
    validate_ps2_raster_settings, donor_candidates, convert_txd_palette_depth,
)
from texture_formats import TXDDocument
from texture_actions import rename_txd_texture


def open_ps2_txd_modify_dialog(host):
    index=host._selected_texture('txd')
    document=getattr(host, 'txd_doc', None)
    if document is None or index is None:
        messagebox.showinfo('Modify PS2 texture', 'Open a PS2 TXD and select a texture first.', parent=host)
        return
    tex=document.textures[index]
    dialog=tk.Toplevel(host)
    dialog.title(f'Modify PS2 texture — {tex.name}')
    dialog.transient(host)
    dialog.geometry('825x515')
    dialog.minsize(720,480)
    dialog.configure(bg='#111827')

    name=tk.StringVar(value=tex.name)
    mode=tk.StringVar(value='Palettized')
    palette=tk.StringVar(value='PAL4' if tex.bpp==4 else 'PAL8')
    pixel_format=tk.StringVar(value='RASTER_8888')
    mip=tk.BooleanVar(value=False)
    scaled=tk.BooleanVar(value=True)
    fill=tk.BooleanVar(value=False)
    background=tk.BooleanVar(value=True)
    status=tk.StringVar(value=f'Original native format: PAL{tex.bpp} with RGBA32 CLUT. No changes made.')

    buttons=ttk.Frame(dialog,padding=(14,10));buttons.pack(side='bottom',fill='x')
    body=ttk.Frame(dialog);body.pack(side='top',fill='both',expand=True)
    left=ttk.Frame(body,padding=(16,12));left.pack(side='left',fill='both',expand=True)
    right=ttk.Frame(body,padding=(12,12));right.pack(side='right',fill='both',expand=True)
    fields=ttk.Frame(left);fields.pack(fill='x')
    fields.columnconfigure(1,weight=1)
    ttk.Label(fields,text='Texture Name').grid(row=0,column=0,sticky='w',pady=5)
    ttk.Entry(fields,textvariable=name,width=25).grid(row=0,column=1,columnspan=2,sticky='ew',padx=(8,0),pady=5)
    ttk.Label(fields,text='Platform').grid(row=1,column=0,sticky='w',pady=5)
    ttk.Label(fields,text='PlayStation 2 · native RenderWare TXD').grid(row=1,column=1,columnspan=2,sticky='w',padx=(8,0))
    ttk.Label(fields,text='Raster Format').grid(row=2,column=0,sticky='w',pady=(11,5))
    ttk.Radiobutton(fields,text='Original',value='Original',variable=mode).grid(row=3,column=0,columnspan=3,sticky='w',pady=3)
    ttk.Radiobutton(fields,text='Raw Raster',value='Raw Raster',variable=mode).grid(row=4,column=0,columnspan=3,sticky='w',pady=3)
    ttk.Radiobutton(fields,text='Palettized',value='Palettized',variable=mode).grid(row=5,column=0,sticky='w',pady=3)
    pal_combo=ttk.Combobox(fields,state='readonly',values=PALETTE_CHOICES,textvariable=palette,width=14)
    pal_combo.grid(row=5,column=1,columnspan=2,sticky='w',padx=(8,0),pady=3)
    ttk.Label(fields,text='Pixel Format').grid(row=6,column=0,sticky='w',pady=(10,5))
    pix_combo=ttk.Combobox(fields,state='readonly',values=PIXEL_FORMAT_CHOICES,textvariable=pixel_format,width=22)
    pix_combo.grid(row=6,column=1,columnspan=2,sticky='w',padx=(8,0),pady=(10,5))
    ttk.Checkbutton(fields,text='Generate mipmaps',variable=mip).grid(row=7,column=0,columnspan=3,sticky='w',pady=(12,4))

    ttk.Separator(left,orient='horizontal').pack(fill='x',pady=(10,9))
    ttk.Label(left,textvariable=status,wraplength=365,justify='left').pack(anchor='w',pady=2)
    ttk.Label(left,text='PAL4 ↔ PAL8 conversion uses an original compatible native raster from the same game/version as its encoding template. Other pixel-format serializers and mipmap generation are not yet validated and cannot be written.',wraplength=355,justify='left').pack(anchor='w',pady=(10,4))
    ttk.Label(left,text='Replacement image input stays TM2: the PNG → TIM2 Converter prepares it first.',wraplength=355,justify='left').pack(anchor='w',pady=(9,4))

    preview_opts=ttk.Frame(right);preview_opts.pack(fill='x')
    ttk.Checkbutton(preview_opts,text='Scaled',variable=scaled).pack(side='left')
    ttk.Checkbutton(preview_opts,text='Fill',variable=fill).pack(side='left',padx=(8,0))
    ttk.Checkbutton(preview_opts,text='Background',variable=background).pack(side='left',padx=(8,0))
    canvas=tk.Canvas(right,width=315,height=315,highlightthickness=1,highlightbackground='#475569',bg='#0f172a')
    canvas.pack(fill='both',expand=True,pady=(8,8))
    ttk.Label(right,text=f'{tex.width}×{tex.height} · PS2 · PAL{tex.bpp} · native 32-bit RGBA palette',wraplength=320,justify='center').pack(anchor='center',pady=7)
    try:
        original_image=tex.image(document.data,host.txd_swizzle.get())
    except Exception as exc:
        original_image=None
        status.set(f'Texture preview unavailable: {exc}')
    last_photo=[None]
    def draw(*_):
        canvas.delete('all')
        if original_image is None:
            canvas.create_text(157,157,text='Preview unavailable',fill='white');return
        img=original_image.convert('RGBA').copy()
        # Preview the color-quantization impact of a requested palette-depth
        # conversion without changing the document until Apply succeeds.
        if mode.get() == 'Palettized' and palette.get() != f'PAL{tex.bpp}':
            new_colors = 16 if palette.get() == 'PAL4' else 256
            img = img.quantize(colors=new_colors, method=Image.Quantize.FASTOCTREE,
                               dither=Image.Dither.NONE).convert('RGBA')
        avail_w=max(20,min(canvas.winfo_width()-16,390))
        avail_h=max(20,min(canvas.winfo_height()-16,390))
        if scaled.get():
            if fill.get():
                ratio=max(avail_w/img.width,avail_h/img.height)
                img=img.resize((max(1,round(img.width*ratio)),max(1,round(img.height*ratio))),Image.Resampling.NEAREST)
                left_crop=max(0,(img.width-avail_w)//2)
                top_crop=max(0,(img.height-avail_h)//2)
                img=img.crop((left_crop,top_crop,left_crop+avail_w,top_crop+avail_h))
            else:
                img.thumbnail((avail_w,avail_h),Image.Resampling.NEAREST)
                multiplier=max(1,min(avail_w//img.width,avail_h//img.height))
                if multiplier>1: img=img.resize((img.width*multiplier,img.height*multiplier),Image.Resampling.NEAREST)
        else:
            img.thumbnail((avail_w,avail_h),Image.Resampling.NEAREST)
        if background.get():
            backdrop=Image.new('RGBA',img.size,(22,27,45,255))
            backdrop.alpha_composite(img)
            img=backdrop.convert('RGB')
        last_photo[0]=ImageTk.PhotoImage(img,master=canvas)
        canvas.create_image(max(1,canvas.winfo_width())//2,max(1,canvas.winfo_height())//2,image=last_photo[0])
    canvas.bind('<Configure>',draw)
    for opt in (scaled,fill,background): opt.trace_add('write',draw)

    def refresh_status(*_):
        if mode.get()=='Original':
            pal_combo.configure(state='disabled');pix_combo.configure(state='disabled')
            status.set('Original keeps all native PS2 raster and palette metadata unchanged.')
            return
        if mode.get()=='Raw Raster':
            pal_combo.configure(state='disabled');pix_combo.configure(state='readonly')
            status.set('Raw Raster appears for compatibility with PS2 TXD editors; creating a direct-color native raster is not implemented yet.')
            return
        pal_combo.configure(state='readonly');pix_combo.configure(state='readonly')
        try:
            new=validate_ps2_raster_settings(tex.bpp,mode=mode.get(),palette=palette.get(),pixel_format=pixel_format.get(),generate_mipmaps=mip.get())
        except ValueError as exc:
            status.set(str(exc));return
        if new!=tex.bpp:
            status.set(f'PAL{tex.bpp} → PAL{new}: requires a same-version native PAL{new} raster template. Colors will be quantized, and the TXD chunk lengths rebuilt.')
        else:
            status.set(f'Preserving PAL{tex.bpp}, RASTER_8888; editing name remains supported. Use Replace TM2 to change its pixels.')
    for var in (mode,palette,pixel_format,mip):var.trace_add('write',refresh_status)
    for var in (mode,palette):var.trace_add('write',draw)

    def apply():
        try:
            new_bpp=validate_ps2_raster_settings(tex.bpp,mode=mode.get(),palette=palette.get(),pixel_format=pixel_format.get(),generate_mipmaps=mip.get())
            candidate=TXDDocument(document.data)
            if new_bpp!=tex.bpp:
                donor=document
                matches=donor_candidates(document,index,donor,new_bpp)
                if not matches:
                    f=filedialog.askopenfilename(parent=dialog,title=f'Choose same-game TXD with {tex.width}×{tex.height} PAL{new_bpp} native raster',filetypes=[('RenderWare TXD files','*.txd'),('All files','*.*')])
                    if not f:return
                    donor=TXDDocument(Path(f).read_bytes())
                    matches=donor_candidates(document,index,donor,new_bpp)
                    if not matches:raise ValueError('No compatible donor raster matched the selected texture dimensions, requested palette depth, and RenderWare version.')
                donor_i=matches[0]
                if not messagebox.askyesno('Confirm PS2 native raster conversion',f'Convert {tex.name} from PAL{tex.bpp} to PAL{new_bpp}?\n\nTemplate: {donor.textures[donor_i].name}\nTarget: {tex.width}×{tex.height}\n\nThe existing image will be re-quantized to {16 if new_bpp==4 else 256} colors, so some color fidelity may change. The original file is not modified.',parent=dialog):return
                candidate=convert_txd_palette_depth(candidate,index,new_bpp,donor,donor_i,swizzled=host.txd_swizzle.get())
            if name.get()!=tex.name:
                rename_txd_texture(candidate,index,name.get())
            if (candidate.textures[index].name,candidate.textures[index].bpp)!=(name.get(),new_bpp):
                raise ValueError('TXD raster write-back failed reparse verification.')
            # Atomic application: the working TXD changes only when all checks pass.
            document.data=candidate.data
            document.textures=candidate.textures
            host._populate_txd_sections()
            if host.txd_tree.exists(f'tex_{index}'):
                host.txd_tree.selection_set(f'tex_{index}');host.txd_tree.focus(f'tex_{index}')
            host.preview_txd()
            host.status.set(f'PS2 texture {name.get()}: PAL{new_bpp} applied in memory. Save edited TXD as a NEW file.')
            dialog.destroy()
        except Exception as exc:
            messagebox.showerror('PS2 raster conversion refused',str(exc)+'\n\nNo changes were applied.',parent=dialog)
    ttk.Button(buttons,text='Cancel',command=dialog.destroy).pack(side='right',padx=(8,0))
    ttk.Button(buttons,text='Modify',style='Accent.TButton',command=apply).pack(side='right')
    dialog.after_idle(draw)
    return dialog
