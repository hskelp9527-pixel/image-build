"""Run a native folder picker in its own main UI thread."""
import sys
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes('-topmost', True)
path = filedialog.askdirectory(title='选择图片文件夹', mustexist=True)
root.destroy()
sys.stdout.buffer.write(path.encode('utf-8'))
