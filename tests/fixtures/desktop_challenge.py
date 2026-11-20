"""Visual-only acceptance app. The random answer is never emitted or saved.

Only this process writes the verdict to its inherited private pipe. The runner
starts it before Codex; the process is not dumpable and has no command API.
"""
import ctypes
import base64
import io
import json
import secrets
import tkinter as tk
from PIL import Image, ImageDraw, ImageFont

ctypes.CDLL(None).prctl(4, 0)  # PR_SET_DUMPABLE: no /proc memory or fd inspection
root = tk.Tk()
root.title('Visual desktop check')
root.geometry('760x480+100+90')
number = str(secrets.randbelow(9000)+1000)
labels=['PINE','LAKE','MOON']
target=secrets.choice(labels)
secrets.SystemRandom().shuffle(labels)
im=Image.new('RGB',(720,170),'white')
d=ImageDraw.Draw(im)
font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',56)
d.text((45,12),number,font=font,fill='black')
d.text((45,86),'Press '+target,font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',32),fill='black')
stream=io.BytesIO(); im.save(stream,format='PNG')
image=tk.PhotoImage(data=base64.b64encode(stream.getvalue()))
tk.Label(root,image=image).pack(pady=16)
entry=tk.Entry(root,font=('DejaVu Sans Mono',26),width=18)
entry.pack(pady=18)
status=tk.Label(root,text='Enter the digits and press the named button.',font=('DejaVu Sans',14))
status.pack()
buttons=tk.Frame(root); buttons.pack(pady=20)
def submit(label):
    ok=entry.get()==number and label==target
    print(json.dumps({'verified':ok}),flush=True)
    status.config(text='SUCCESS' if ok else 'Try again',fg='green' if ok else 'red')
for label in labels:
    tk.Button(buttons,text=label,font=('DejaVu Sans',18),width=10,command=lambda label=label:submit(label)).pack(side='left',padx=12)
root.mainloop()
