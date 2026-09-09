"""Cross-selector reopening and additive feature activation with independent participants."""

import pytest

from okto_grafx import connect, TextIndexOptions


@pytest.mark.parametrize("descriptor", ["strict", "generation"])
def test_v005_additive_activation_pure_reopen(tmp_path, descriptor):
    path = tmp_path / "db"
    options = dict(checksum="pure", codec="pure", vector_math="pure", descriptor_revalidation=descriptor)
    with connect(path, **options) as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE D(id INT64,body STRING,PRIMARY KEY(id))")
            tx.execute("CREATE (:D {id:1,body:'searchable text'})")
        db.create_index("body", "D", ("body",), layout="sparse_hash", bucket_count=128)
        db.create_text_index("fts", "D", ("body",), options=TextIndexOptions(statistics_mode="durable", statistics_history_entries=2))
        db.maintenance.vacuum(confirm_quiescent=True, index_free_pages=True)
    with connect(path, **options) as db, connect(path, **options) as writer:
        with db.begin("read") as old:
            with writer.begin() as tx:
                tx.execute("MATCH (d:D {id:1}) SET d.body='changed text'")
            assert old.execute("MATCH (d:D) RETURN d.body").rows == (("searchable text",),)
            assert db.execute("MATCH (d:D) WHERE d.body='changed text' RETURN d.id").rows == ((1,),)
        assert db.search_text(index="fts", query="changed").corpus_documents == 1
        assert not db.verify("all").findings


@pytest.mark.optional_dependency("numpy")
@pytest.mark.optional_dependency("google_crc32c")
def test_accelerated_writer_pure_reader_byte_contract(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("google_crc32c")
    path = tmp_path / "db"
    with connect(path, checksum="native", codec="numpy", vector_math="numpy") as db:
        with db.begin() as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE D(id INT64,v VECTOR(s),PRIMARY KEY(id))")
            tx.execute("CREATE (:D {id:1,v:[1.0,0.0]})")
        db.checkpoint()
    with connect(path, checksum="pure", codec="pure", vector_math="pure") as db:
        with db.begin("read") as reader:
            assert len(db.search_vectors(reader, space="s", query=(1.0, 0.0), k=1).hits) == 1
        assert not db.verify("all").findings
