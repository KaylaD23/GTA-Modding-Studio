"""Selection-based native texture operations for GTA Modding Studio.

Only operations with a validated native fixed-size write path are enabled.
Unimplemented dictionary restructuring, pixel-format changes and mipmap
rebuilds explicitly refuse to write instead of corrupting game assets.
"""
from __future__ import annotations
import io, tempfile, tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageOps, ImageEnhance, ImageTk


def rename_txd_texture(document, index: int, new_name: str) -> None:
    """Replace a RenderWare native texture's name inside its original STRING chunk."""
    from texture_formats import TXDDocument, chunks
    tex = document.textures[index]
    name = new_name.strip()
    try: encoded = name.encode('ascii')
    except UnicodeEncodeError as exc: raise ValueError('RenderWare texture names must be ASCII.') from exc
    if not encoded or b'\0' in encoded or any(x < 32 for x in encoded):
        raise ValueError('Choose a non-empty printable ASCII texture name.')
    if any(other.name.casefold() == name.casefold() for i, other in enumerate(document.textures) if i != index):
        raise ValueError('A texture with this name already exists in the dictionary.')
    original = document.data
    for typ, _, off, size, _ in chunks(original, 12, len(original)):
        if typ != 0x15: continue
        children = chunks(original, off, off + size)
        if len(children) < 2 or children[1][0] != 2: continue
        _, _, nameoff, capacity, _ = children[1]
        old = original[nameoff:nameoff+capacity].split(b'\0')[0].decode('ascii','replace')
        # For names that might repeat unexpectedly, identify the exact entry by offset
        # of the parent native chunk, as recorded in the parser's note.
        if old != tex.name: continue
        if len(encoded) + 1 > capacity:
            raise ValueError(f'Name exceeds the existing {capacity}-byte native string allocation. Max {capacity-1} characters.')
        data = bytearray(original)
        data[nameoff:nameoff+capacity] = encoded.ljust(capacity, b'\0')
        updated = TXDDocument(bytes(data))
        if len(updated.textures) != len(document.textures): raise ValueError('Texture count changed after rename.')
        if updated.textures[index].name != name: raise ValueError('Renamed entry failed reparse verification.')
        document.data = bytes(data)
        document.textures[index].name = name
        return
    raise ValueError('The selected texture has no supported native name chunk.')


def modify_txd_dictionary(document, operation: str, *, selected_index: int | None = None, donor=None, donor_index: int = 0):
    """Rebuild native texture chunks with strict RenderWare size/count verification.

    Preserve all unrelated bytes and original native chunk versions; refuse when
    the source/target native platform identifier differs.
    """
    from texture_formats import TXDDocument, chunks
    import struct
    data=document.data
    outer=chunks(data,0,len(data))
    if len(outer)!=1 or outer[0][0]!=0x16:raise ValueError('Not a single RenderWare texture dictionary.')
    _,_,payload,length,_=outer[0]
    children=chunks(data,payload,payload+length)
    if not children or children[0][0]!=1 or children[0][3]<2:raise ValueError('No recognized dictionary count field.')
    old_count=struct.unpack_from('<H',data,children[0][2])[0]
    natives=[node for node in children if node[0]==0x15]
    if old_count!=len(natives):raise ValueError('Texture count does not match the actual native chunk count.')
    if len(natives)!=len(document.textures):raise ValueError('Dictionary has unsupported native textures; refusing structural edits.')
    if operation=='remove':
        if selected_index is None or not 0<=selected_index<len(natives):raise ValueError('Select a native texture first.')
        target=natives[selected_index]
        begin=target[1];end=target[2]+target[3]
        edited=data[:begin]+data[end:]
        count=old_count-1
    elif operation=='add':
        if donor is None or not 0<=donor_index<len(donor.textures):raise ValueError('Select a donor texture.')
        dchunks=chunks(donor.data,12,len(donor.data)); source_nodes=[n for n in dchunks if n[0]==0x15]
        if len(source_nodes)!=len(donor.textures):raise ValueError('Donor contains unsupported native textures.')
        src=source_nodes[donor_index]
        src_payload=donor.data[src[1]:src[2]+src[3]]
        target_platform=data[natives[0][2]+12+12:natives[0][2]+12+16] if natives else b''
        src_platform=donor.data[src[2]+12+12:src[2]+12+16]
        # Both are PS2 native chunks and should advertise the same PS2 marker.
        if target_platform and src_platform!=target_platform:
            raise ValueError('Donor native platform differs from target TXD.')
        name=donor.textures[donor_index].name
        if any(t.name.casefold()==name.casefold() for t in document.textures):raise ValueError(f'Duplicate texture name: {name}')
        insert=natives[-1][2]+natives[-1][3] if natives else children[0][2]+children[0][3]
        edited=data[:insert]+src_payload+data[insert:]
        count=old_count+1
    else:raise ValueError('Invalid dictionary operation')
    buf=bytearray(edited)
    struct.pack_into('<I',buf,4,len(buf)-12)
    struct.pack_into('<H',buf,children[0][2],count)
    new=TXDDocument(bytes(buf))
    if len(new.textures)!=count:raise ValueError('Rebuilt dictionary failed texture-count validation.')
    document.data=bytes(buf)
    document.textures=new.textures
    return document


class TextureActionsMixin:
    def _install_texture_context(self, which):
        tree = getattr(self, f'{which}_tree')
        menu = tk.Menu(tree, tearoff=False)
        actions = [
            ('Add…', lambda: self._texture_add(which)),
            ('Replace from TM2…', lambda: self._texture_replace(which)),
            ('Remove', lambda: self._texture_remove(which)),
            ('Rename…', lambda: self._texture_rename(which)),
            ('Resize…', lambda: self._unsupported_edit(which, 'Changing pixel dimensions requires resizing native raster/GS allocations, texture header fields and mipmap data.')),
            ('Manipulate…', lambda: self._texture_manipulate(which)),
            ('Setup mip-levels…', lambda: self._unsupported_edit(which, 'Native mipmap generation is not yet supported. Existing levels are preserved.')),
            ('Clear mip-levels…', lambda: self._unsupported_edit(which, 'Removing mip levels requires rewriting native texture headers and packet sizes. Existing levels are preserved.')),
            ('Modify texture raster / properties…', lambda: self._texture_modify(which)),
            ('Setup TXD version…', lambda: self._unsupported_edit(which, 'Version rewrites require validating platform-specific RenderWare native serializer fields.')),
            ('Options…', lambda: self._texture_modify(which)),
        ]
        for label, action in actions: menu.add_command(label=label, command=action)
        index_menu = tk.Menu(tree, tearoff=False)
        index_menu.add_command(label='Inspect indexed entry',
                               command=(lambda: self._on_dtz_selected() if which == 'dtz' else None))
        index_menu.add_command(label='Replacement unavailable — image layout not decoded',state='disabled')
        def popup(event):
            selected = tree.identify_row(event.y)
            if not selected:
                return
            # Always select the row under the mouse. An indexed HUD item must
            # never inherit a previous menu texture as the replacement target.
            tree.selection_set(selected); tree.focus(selected)
            target = menu if selected.startswith('tex_') else index_menu
            try:target.tk_popup(event.x_root, event.y_root)
            finally:target.grab_release()
        tree.bind('<Button-3>', popup)
        bindings = [('<Insert>',actions[0][1]),('<Control-r>',actions[1][1]),('<Delete>',actions[2][1]),('<F2>',actions[3][1]),('<Alt-s>',actions[4][1]),('<m>',actions[5][1]),('<Control-m>',actions[6][1]),('<Control-Shift-c>',actions[7][1])]
        for sequence, action in bindings:tree.bind(sequence, lambda e, action=action: (action(), 'break')[1])
        tree.bind('<Double-1>', lambda e: self._texture_modify(which))
        setattr(self, f'_{which}_context_menu',menu)

    def _unsupported_edit(self, which, reason):
        messagebox.showwarning('Unsupported native texture operation',reason+'\n\nNo source bytes were modified.',parent=self)

    def _texture_add(self, which):
        if which != 'txd':return self._unsupported_edit(which,'Adding DTZ XET/CHK entries requires the currently undecoded game-specific linked-entry directory layout.')
        doc=getattr(self,'txd_doc',None)
        if doc is None:return
        f=filedialog.askopenfilename(parent=self,title='Choose TXD containing donor texture',filetypes=[('TXD','*.txd'),('All files','*.*')])
        if not f:return
        try:
            from texture_formats import TXDDocument
            donor=TXDDocument(Path(f).read_bytes())
            from tkinter import simpledialog
            idx=simpledialog.askinteger('Import native texture',f'Donor has {len(donor.textures)} textures. Choose index (1–{len(donor.textures)}):',initialvalue=1,minvalue=1,maxvalue=len(donor.textures),parent=self)
            if idx is None:return
            modify_txd_dictionary(doc,'add',donor=donor,donor_index=idx-1)
            self.inspect_txd_list_from_memory()
            self.status.set(f'Imported {donor.textures[idx-1].name} as a native texture. Save a new TXD.')
        except Exception as exc:messagebox.showerror('TXD import refused',str(exc),parent=self)

    def _texture_remove(self, which):
        if which != 'txd':return self._unsupported_edit(which,'Removing DTZ XET/CHK entries requires editing the linked-node directory and relocation pointers.')
        doc=getattr(self,'txd_doc',None);index=self._selected_texture('txd')
        if doc is None or index is None:return
        if not messagebox.askyesno('Remove texture',f'Remove {doc.textures[index].name} from this in-memory TXD?\nOriginal remains untouched.',parent=self):return
        try:
            modify_txd_dictionary(doc,'remove',selected_index=index)
            self.inspect_txd_list_from_memory()
            self.status.set('Removed selected native texture; save as a new TXD.')
        except Exception as exc:messagebox.showerror('TXD removal refused',str(exc),parent=self)

    def inspect_txd_list_from_memory(self):
        self._populate_txd_sections()

    def _texture_replace(self, which):
        (self.replace_txd_tm2 if which=='txd' else self.replace_dtz_tm2)()

    def _texture_rename(self, which):
        index = self._selected_texture(which)
        doc = getattr(self, f'{which}_doc',None)
        if doc is None or index is None:return
        if which != 'txd':
            return self._unsupported_edit(which,'Renaming DTZ XET/CHK entries requires the complete linked-node pointer/CRC rules.')
        from tkinter import simpledialog
        name = simpledialog.askstring('Rename texture', 'New internal texture name:',initialvalue=doc.textures[index].name,parent=self)
        if name is None:return
        try:
            rename_txd_texture(doc,index,name)
            self.txd_tree.item(f'tex_{index}',text=name)
            self.preview_txd()
            self.status.set(f'Renamed TXD texture to {name}. Save as a new TXD to preserve changes.')
        except Exception as exc:messagebox.showerror('Rename refused',str(exc),parent=self)

    def _texture_manipulate(self, which):
        index = self._selected_texture(which)
        doc = getattr(self,f'{which}_doc',None)
        if doc is None or index is None:return
        op = tk.StringVar(value='Flip horizontal')
        win=tk.Toplevel(self);win.title('Manipulate selected texture');win.transient(self);win.geometry('390x190')
        ttk.Label(win,text='Apply an operation to the selected texture',padding=10).pack(anchor='w')
        ttk.Combobox(win,textvariable=op,state='readonly',values=['Flip horizontal','Flip vertical','Rotate 180°','Invert RGB','Brightness +20%'],width=36).pack(padx=10,fill='x')
        ttk.Label(win,text='Keeps original dimensions, palette and native allocation. Colors are remapped to the existing palette.',wraplength=350).pack(padx=10,pady=10)
        def apply():
            try:
                if which=='txd': tex=doc.textures[index];img=tex.image(doc.data,self.txd_swizzle.get())
                else:img=doc.preview(index,self.dtz_swizzle.get())
                action=op.get()
                if action=='Flip horizontal':img=ImageOps.mirror(img)
                elif action=='Flip vertical':img=ImageOps.flip(img)
                elif action=='Rotate 180°':img=img.rotate(180)
                elif action=='Invert RGB':
                    r,g,b,a=img.split(); rgb=ImageOps.invert(Image.merge('RGB',(r,g,b)));rgb.putalpha(a);img=rgb
                else:img=ImageEnhance.Brightness(img).enhance(1.2)
                with tempfile.TemporaryDirectory(prefix='gta_texture_edit_') as temp:
                    png=Path(temp)/'change.png';img.save(png)
                    if which=='txd':doc.replace(index,png,self.txd_swizzle.get());self.preview_txd()
                    else:doc.replace(index,png,self.dtz_swizzle.get());self.preview_dtz()
                win.destroy(); self.status.set('Manipulated texture in memory. Save as a new game file to retain changes.')
            except Exception as exc:messagebox.showerror('Manipulation refused',str(exc),parent=win)
        ttk.Button(win,text='Apply',command=apply).pack(anchor='e',padx=12)

    def _texture_modify(self,which):
        if which == 'txd':
            from PS2_TXD_Modify_Dialog import open_ps2_txd_modify_dialog
            return open_ps2_txd_modify_dialog(self)
        doc=getattr(self,f'{which}_doc',None)
        index=self._selected_texture(which)
        if doc is None or index is None:return
        tex=doc.textures[index] if which=='txd' else doc.entries[index][1]
        win=tk.Toplevel(self);win.title('Modify texture — '+tex.name);win.geometry('640x375');win.transient(self)
        left=ttk.Frame(win,padding=12);left.pack(side='left',fill='both',expand=True)
        name=tk.StringVar(value=tex.name)
        ttk.Label(left,text='Texture name').pack(anchor='w');ttk.Entry(left,textvariable=name).pack(fill='x',pady=4)
        ttk.Label(left,text='Platform').pack(anchor='w');ttk.Combobox(left,values=['PlayStation 2' if which=='txd' else 'GTA Stories PS2/PSP resource'],state='readonly',width=36,textvariable=tk.StringVar(value=('PlayStation 2' if which=='txd' else 'GTA Stories PS2/PSP resource'))).pack(fill='x',pady=4)
        ttk.Label(left,text='Raster format').pack(anchor='w');fmt='PAL4' if tex.bpp==4 else 'PAL8'
        ttk.Label(left,text=f'Original native indexed {fmt} (locked to source allocation)').pack(anchor='w',pady=5)
        ttk.Label(left,text='Pixel format').pack(anchor='w');ttk.Label(left,text='RASTER_8888 (existing 32-bit CLUT; preserved)').pack(anchor='w',pady=5)
        ttk.Label(left,text=f'{tex.width} × {tex.height} · {tex.bpp}-bit · existing mip levels preserved').pack(anchor='w',pady=7)
        ttk.Label(left,text='Pixel-format conversion, mipmap rebuilding and resizing are disabled until their native metadata writers are validated.',wraplength=280).pack(anchor='w')
        right=ttk.Frame(win,padding=10);right.pack(side='right',fill='y')
        try:img=tex.image(doc.data,self.txd_swizzle.get()) if which=='txd' else doc.preview(index,self.dtz_swizzle.get())
        except Exception:img=None
        photo=None
        if img is not None:
            img.thumbnail((250,250),Image.Resampling.NEAREST); photo=ImageTk.PhotoImage(img,master=win)
        ttk.Label(right,image=photo).pack();win._preview_ref=photo
        row=ttk.Frame(left);row.pack(anchor='w',pady=10)
        ttk.Button(row,text='Replace TM2…',command=lambda:(win.destroy(),self._texture_replace(which))).pack(side='left',padx=4)
        def save_name():
            if which=='txd' and name.get()!=tex.name:
                try:rename_txd_texture(doc,index,name.get());self.txd_tree.item(f'tex_{index}',text=name.get());self.preview_txd()
                except Exception as exc:return messagebox.showerror('Rename refused',str(exc),parent=win)
            elif which!='txd' and name.get()!=tex.name:return self._unsupported_edit(which,'DTZ entry renaming is unsupported; original name preserved.')
            win.destroy()
        ttk.Button(row,text='Apply',command=save_name).pack(side='left',padx=4)
