from sqlalchemy import URL, create_engine, text

connection_string = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    r"SERVER=localhost\SQLEXPRESS;"
    "DATABASE=MiniQuantDemo;"
    "Trusted_Connection=yes;"
    "Encrypt=yes;TrustServerCertificate=yes;"
)

engine = create_engine(URL.create(
    "mssql+pyodbc", query={"odbc_connect": connection_string}
))

with engine.connect() as conn:
    print("Database:", conn.execute(text("SELECT DB_NAME()")).scalar_one())
    print("Rows:", conn.execute(text(
        "SELECT COUNT(*) FROM dbo.TWSE_ALL_202611"
    )).scalar_one())

