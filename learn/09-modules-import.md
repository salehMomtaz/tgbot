# Lesson 09 — Modules & import

A **module** is just a Python file. When you `import` it, you can use its
variables, functions, and classes.

```python
# config.py
API_ID = 0
BOT_TOKEN = ""

# main.py
import config
print(config.BOT_TOKEN)
```

## Four styles of import

```python
import math               # use: math.sqrt(9)
from math import sqrt      # use: sqrt(9)
from math import sqrt, pi  # multiple names
import math as m           # alias: m.sqrt(9)
```

`from … import *` is technically legal but it pollutes your namespace. Avoid it.

## Python finds modules via sys.path

When you `import config`, Python looks in:

1. The current directory (the folder of the script you ran).
2. Each entry in `sys.path`.
3. Standard library paths.

This is why `import config` works in this bot: `main.py` is run from the
project root, so `config.py` in the same folder is found.

## Your own packages

A folder with `__init__.py` is a **package**. `modules/` and `utils/` are
packages here:

```
tgbot/
├── main.py
├── config.py
├── modules/
│   ├── __init__.py
│   ├── admin.py
│   └── downloader_handler.py
└── utils/
    ├── __init__.py
    ├── downloader.py
    └── pot_provider.py
```

To import `extract_formats` from `utils.downloader`:

```python
from utils.downloader import extract_formats
```

The dot means "go down one level". The folder name is the package name; the
file name is the module name.

## `if __name__ == "__main__":`

Every `.py` file has a hidden variable `__name__`. When you *run* a file, it is
`"__main__"`. When you *import* it, it is the file's name.

```python
# at the bottom of main.py
if __name__ == "__main__":
    main()
```

This lets a file double as a script and as a library — important here:
`config.py` is imported by *every* module, but you can also `python config.py`
to sanity-check the import.

## Where this shows up in tgbot

`config.py` puts `load_dotenv()` at the very top:

```python
from dotenv import load_dotenv
load_dotenv()
```

This means: the moment any module does `import config`, dotenv runs. The order
of imports does not matter — `config.py` is self-sufficient.

`main.py::main_engine` is full of intra-project imports that mirror the file
map:

```python
from modules.admin import register_admin_handlers
from modules.downloader_handler import register_downloader_handlers
from modules.stream_interceptor import register_stream_interceptor_handlers
```

## Exercise

Create `mylib.py` in a new folder with one function `double(x)` that returns
`x * 2`. Create `app.py` in the same folder that imports it and prints
`double(7)`. Run `python app.py`.
