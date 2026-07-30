"""Lesson 14 — solution

Run: python3 14_solution.py
Expected: 3 results, total elapsed ~ 3.0s (not 6.0s)
"""

import asyncio
import time

async def slow_greet(name: str, delay: float) -> str:
    await asyncio.sleep(delay)
    return f"Hello, {name} (waited {delay}s)"

async def main():
    t0 = time.perf_counter()
    results = await asyncio.gather(
        slow_greet("A", 1.0),
        slow_greet("B", 2.0),
        slow_greet("C", 3.0),
    )
    elapsed = time.perf_counter() - t0
    for r in results:
        print(r)
    print(f"Total elapsed: {elapsed:.1f}s")

if __name__ == "__main__":
    asyncio.run(main())
