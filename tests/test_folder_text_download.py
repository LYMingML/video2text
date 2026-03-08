import io
import zipfile

from fastapi.testclient import TestClient

import fastapi_app


client = TestClient(fastapi_app.app)


def test_download_history_text_returns_zip_with_all_texts(monkeypatch, tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    folder = workspace / 'job1'
    folder.mkdir()
    (folder / 'a.txt').write_text('A', encoding='utf-8')
    (folder / 'b.txt').write_text('B', encoding='utf-8')
    (folder / 'ignore.srt').write_text('SRT', encoding='utf-8')

    monkeypatch.setattr(fastapi_app.core, 'WORKSPACE_DIR', workspace)

    resp = client.get('/api/folders/download-text', params={'folder_name': 'job1'})

    assert resp.status_code == 200
    assert resp.headers['content-type'] == 'application/zip'
    assert 'job1.texts.zip' in resp.headers.get('content-disposition', '')

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert sorted(zf.namelist()) == ['a.txt', 'b.txt']
        assert zf.read('a.txt').decode('utf-8') == 'A'
        assert zf.read('b.txt').decode('utf-8') == 'B'