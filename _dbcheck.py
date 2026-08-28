
import asyncio
import asyncpg

async def main():
    try:
        # connect to the default 'dnd' database (postgres image default) to list databases
        conn = await asyncpg.connect(host="localhost", port=5432, user="dnd", password="dnd", database="dnd")
    except Exception as e:
        print("CONNECT_DEFAULT_FAILED:", type(e).__name__, e)
        return
    try:
        rows = await conn.fetch("SELECT datname FROM pg_database ORDER BY datname")
        print("DATABASES:", [r["datname"] for r in rows])
    finally:
        await conn.close()

asyncio.run(main())
