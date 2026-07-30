# Lesson 13 — Classes & OOP

A **class** is a blueprint. An **instance** is a specific copy built from that
blueprint. You are going to meet classes in this bot because pyrogram (the
Telegram library) is written with them.

## Define a class

```python
class Dog:
    species = "Canine"   # shared by all Dogs (class attribute)

    def __init__(self, name: str):  # runs when you create an instance
        self.name = name             # instance attribute

    def bark(self):
        return f"{self.name} says Woof!"

rex = Dog("Rex")
print(rex.bark())       # Rex says Woof!
print(rex.species)      # Canine
print(Dog.species)      # Canine (also on the class)
```

`__init__` is the constructor. `self` is the "this" of Python — it points to
the current instance when a method is called.

## Inheritance

```python
class GuideDog(Dog):
    def bark(self):
        return f"{self.name} whispers Woof"  # override

sparky = GuideDog("Sparky")
```

`isinstance(sparky, Dog)` is `True` — Sparky is also a Dog.

## You do not need many classes to use libraries

Many beginner Python tutorials stress classes, but in this bot, classes are used
only because pyrogram uses them. Our code is almost entirely functions. This is
fine. Pyramidal libraries (Telegram clients, HTTP clients) tend to be classes;
your application code can be functional.

## Where this shows up in tgbot

When a message arrives, pyrogram gives you a `Message` object (an instance):

```python
@app.on_message(filters.private, group=-2)
async def handler(client: Client, message: Message):
    # message is a Message instance
    # client is a Client instance
    user_id = message.from_user.id
    text = message.text
```

`Client` (the bot itself), `Message` (what a message is), `CallbackQuery`
(what a button press is), `InlineKeyboardButton` (what a keyboard button is)
— these are all classes defined by pyrogram. We use their methods (`.reply_text`,
`.edit_text`, `.answer`, `continue_propagation`, ...) without caring about
their internals.

The PO-token provider manager is a class too (`utils/pot_provider.py`):

```python
class PotProviderManager:
    def __init__(self):
        self.server_path = ...
    async def start(self): ...
    async def stop(self): ...
```

Here we do *care* about the internals — we wrote them — so we use the class
shape to hold state (`is_running`, the process handle, the port).

## Exercise

Create a class `CookieJar` with:
- `__init__(self, path)` stores `self.path`.
- `exists()` returns `True` if the file exists (`os.path.exists`).
- `size_bytes()` returns its size (or `0` if missing).
- `read()` returns the file contents (or `""` if missing).

Create an instance pointed at a real cookie jar in the bot and call all three.
