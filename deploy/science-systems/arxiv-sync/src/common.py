"""arXiv の最新化ジョブの共通部品.

- 書き込みの方式: 新しい行は parquet にして RustFS に置き, Trino の add_files で Iceberg テーブルに登録する
  (pdf / source などの大きな中身を Trino に通さないため). 更新・削除は Trino の MERGE / DELETE (merge-on-read) で行う.
  add_files で登録したテーブル (pdf / source / latex / paper_text / ps) の既存の parquet は書き換えない
- 更新用の一時テーブルは名前空間 arxiv_staging に作り, 使い終わったら登録を外してファイルを消す
"""

import logging
import os
import re
import sys
import time
import uuid

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq
import trino

log = logging.getLogger("arxiv_sync")

WAREHOUSE = "arxiv-warehouse"
RAW = "arxiv-raw"


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
    )
    logging.Formatter.converter = time.gmtime


def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        raise SystemExit(f"environment variable {name} is required")
    return v


def run_id():
    return time.strftime("%Y%m%dt%H%M%S", time.gmtime()) + uuid.uuid4().hex[:6]


# ---------------------------------------------------------------- RustFS (S3)

def rustfs():
    return pafs.S3FileSystem(
        endpoint_override=env("SYNC_RUSTFS_ENDPOINT", "http://science-rustfs-svc.science-systems.svc:9000"),
        access_key=env("RUSTFS_ACCESS_KEY"),
        secret_key=env("RUSTFS_SECRET_KEY"),
        region="us-east-1",
        scheme="http",
        allow_bucket_creation=False,
        request_timeout=300,
        connect_timeout=30,
    )


def put_bytes(fs, path, data):
    with fs.open_output_stream(path) as f:
        f.write(data)


def delete_prefix(fs, path):
    try:
        fs.delete_dir(path)
    except FileNotFoundError:
        pass


def write_parquet(fs, path, table, row_group_size):
    """ZSTD の parquet を書く. 中身の列 (binary) は行グループを小さくする (元の HF の parquet は 200 行程度)."""
    pq.write_table(
        table,
        path,
        filesystem=fs,
        compression="zstd",
        row_group_size=row_group_size,
        write_statistics=True,
    )
    log.info("wrote %s (%d rows)", path, table.num_rows)


# ---------------------------------------------------------------- Trino

class Trino:
    def __init__(self):
        self.conn = trino.dbapi.connect(
            host=env("SYNC_TRINO_HOST", "trino.cluster.orenet.net"),
            port=int(env("SYNC_TRINO_PORT", "443")),
            http_scheme="https",
            user=env("SYNC_TRINO_USER", "admin"),
            auth=trino.auth.BasicAuthentication(env("SYNC_TRINO_USER", "admin"), env("TRINO_PASSWORD")),
            catalog="iceberg",
            request_timeout=3600,
        )

    def q(self, sql, params=None):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        return rows

    def one(self, sql, params=None):
        rows = self.q(sql, params)
        return rows[0][0] if rows else None

    def exists(self, schema, table):
        return self.one(
            "SELECT count(*) FROM iceberg.information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ) == 1


# Iceberg (Trino) の型 ← arrow の型
def trino_type(t):
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "varchar"
    if pa.types.is_binary(t) or pa.types.is_large_binary(t):
        return "varbinary"
    if pa.types.is_int32(t):
        return "integer"
    if pa.types.is_int64(t):
        return "bigint"
    if pa.types.is_boolean(t):
        return "boolean"
    if pa.types.is_timestamp(t):
        return "timestamp(6)"
    if pa.types.is_list(t) or pa.types.is_large_list(t):
        return f"array({trino_type(t.value_type)})"
    raise ValueError(f"unsupported type {t}")


_SAFE = re.compile(r"^[a-z0-9_]+$")


class Staging:
    """arxiv_staging の一時テーブル. parquet を置いて add_files で登録し, 使い終わったら消す."""

    def __init__(self, tr, fs, name, schema):
        assert _SAFE.match(name), name
        self.tr, self.fs, self.name, self.schema = tr, fs, name, schema
        self.base = f"{WAREHOUSE}/arxiv_staging/{name}"
        self.table = f"iceberg.arxiv_staging.{name}"

    def load(self, tables, row_group_size=10000):
        """tables: pyarrow.Table のリスト. parquet に書いて登録する. 行がなければ False."""
        tables = [t for t in tables if t.num_rows]
        if not tables:
            return False
        self.tr.q("CREATE SCHEMA IF NOT EXISTS iceberg.arxiv_staging")
        for i, t in enumerate(tables):
            write_parquet(self.fs, f"{self.base}/data/part-{i:05d}.parquet", t.cast(self.schema), row_group_size)
        cols = ", ".join(f'"{f.name}" {trino_type(f.type)}' for f in self.schema)
        self.tr.q(f"CREATE TABLE {self.table} ({cols}) WITH (location = 's3://{self.base}/iceberg')")
        self.tr.q(
            f"ALTER TABLE {self.table} EXECUTE add_files(location => 's3://{self.base}/data/', format => 'PARQUET')"
        )
        return True

    def drop(self):
        if self.tr.exists("arxiv_staging", self.name):
            self.tr.q(f"CALL iceberg.system.unregister_table(schema_name => 'arxiv_staging', table_name => '{self.name}')")
        delete_prefix(self.fs, self.base)


def add_files(tr, fs, table, prefix, tables, row_group_size):
    """新しい行を parquet にして iceberg.arxiv.<table> に add_files で追加する (既存のファイルは書き換えない).

    prefix: s3://arxiv-warehouse/ からの相対パス (例: arxiv/pdf/data/sync/<run>). テーブルの場所の外でもよい.
    """
    n = 0
    for i, t in enumerate(tables):
        if t.num_rows:
            write_parquet(fs, f"{WAREHOUSE}/{prefix}/part-{i:05d}.parquet", t, row_group_size)
            n += t.num_rows
    if n:
        tr.q(f"ALTER TABLE iceberg.arxiv.{table} EXECUTE add_files(location => 's3://{WAREHOUSE}/{prefix}/', format => 'PARQUET')")
        log.info("add_files %s: %d rows from %s", table, n, prefix)
    return n
