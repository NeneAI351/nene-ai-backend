"""Provider request contracts, without spending credits or calling LTX."""
from unittest.mock import AsyncMock

import httpx
import pytest

import generation_service
import generation_worker as worker
from ltx_contract import output_resolution

OWNER = '11111111-1111-4111-8111-111111111111'
ASSET = '22222222-2222-4222-8222-222222222222'


@pytest.mark.parametrize('quality,expected', [
    ('720p', ['1280x720','720x1280','720x720','1080x720','720x1080']),
    ('1080p', ['1920x1080','1080x1920','1080x1080','1620x1080','1080x1620']),
    ('1440p', ['2560x1440','1440x2560','1440x1440','2160x1440','1440x2160']),
    ('4k', ['3840x2160','2160x3840','2160x2160','3240x2160','2160x3240']),
])
def test_documented_pixel_sizes(quality, expected):
    assert [output_resolution(quality, ratio) for ratio in ['16:9','9:16','1:1','3:2','2:3']] == expected
    assert output_resolution(quality) == expected[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['text-to-video','image-to-video'])
@pytest.mark.parametrize('quality,ratio,expected', [
    ('720p','16:9','1280x720'), ('720p','9:16','720x1280'), ('720p','1:1','720x720'),
    ('1080p','16:9','1920x1080'), ('1080p','9:16','1080x1920'), ('1080p','1:1','1080x1080'),
])
async def test_dispatch_translates_dimensions_without_changing_billable_settings(monkeypatch, kind, quality, ratio, expected):
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, *, headers, json):
            calls.append((url,json))
            return httpx.Response(202,json={'id':'provider-job'})
    monkeypatch.setattr(worker.httpx,'AsyncClient',Client)
    monkeypatch.setattr(worker,'_provider_config',lambda:('https://provider.test',{}))
    begin=AsyncMock()
    monkeypatch.setattr(worker.generation_store,'begin_submission',begin)
    monkeypatch.setattr(worker.generation_store,'attach_provider_job',AsyncMock())
    monkeypatch.setattr(worker.generation_outbox,'enqueue_generation_event',AsyncMock())
    monkeypatch.setattr(worker.database,'fetch_one',AsyncMock(return_value={'object_key':f'source-images/{OWNER}/{ASSET}.png'}))
    monkeypatch.setattr(worker.media_storage,'create_signed_source_image_url',lambda *args:'https://storage.test/private-image')
    request={'prompt':'A river','model':'ltx-2-5-fast','resolution':quality,'duration':10,'aspect_ratio':ratio,'generate_audio':False}
    if kind=='image-to-video': request['source_image_id']=ASSET
    await worker._dispatch({'id':ASSET,'user_id':OWNER,'provider':'ltx','status':'queued','generation_type':kind,'request':request})
    assert len(calls)==1
    url,body=calls[0]
    assert url==f'https://provider.test/v2/{kind}'
    assert body['resolution']==expected
    assert 'aspect_ratio' not in body
    assert body['duration']==10 and body['generate_audio'] is False
    assert body['model']=='ltx-2-5-fast'
    assert request['resolution']==quality and request['aspect_ratio']==ratio
    assert begin.await_count==1
    if kind=='image-to-video': assert body['image_uri']=='https://storage.test/private-image'


@pytest.mark.asyncio
@pytest.mark.parametrize('ratio', ['21:9','4:3','3:4','4:5'])
async def test_unsupported_dimensions_rejected_before_credit_transaction(monkeypatch, ratio):
    class NoTransaction:
        def connection(self): raise AssertionError('Cannot reserve credits for an unsupported provider shape')
    monkeypatch.setattr(generation_service.database,'_pool',NoTransaction())
    with pytest.raises(ValueError,match='Unsupported LTX'):
        await generation_service.create_reserved_generation(
            OWNER,idempotency_key='shape-check',generation_type='text-to-video',prompt='A river',
            provider='ltx',model='ltx-2-5-fast',resolution='720p',duration_seconds=6,aspect_ratio=ratio)
