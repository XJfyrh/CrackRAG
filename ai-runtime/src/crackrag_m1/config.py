from dataclasses import dataclass
from pathlib import Path
import os

VERSION = 'm3-runtime-v1'
REVISION = '5617a9f61b028005a4858fdac845db406aefb181'
EMBEDDING_VERSION = 'bge-m3:'+REVISION+':dense-cls-l2:512:overlap64:v1'
ROOT = Path(__file__).resolve().parents[3]

@dataclass(frozen=True)
class Settings:
    address: str
    tools_address: str
    internal_token: str
    provider: str
    embedding_mode: str
    embedding_directory: Path
    price_snapshot: Path
    mock_scenario: str
    release_manifest: str = ''
    release_root: str = ''
    live_session: str = ''
    live_control: str = ''

    @classmethod
    def load(cls):
        if os.getenv('M1_INTERNAL_TOKEN_FILE'):
            os.environ['M1_INTERNAL_TOKEN']=Path(os.environ['M1_INTERNAL_TOKEN_FILE']).read_text(encoding='utf-8').strip()
        result = cls(os.getenv('M1_RUNTIME_ADDRESS','127.0.0.1:50051'),
            os.getenv('M1_DATA_TOOLS_ADDRESS','127.0.0.1:50052'),
            os.getenv('M1_INTERNAL_TOKEN','local-m1-service-token'),
            os.getenv('M1_MODEL_PROVIDER','mock'), os.getenv('M1_EMBEDDING_MODE','bge-m3'),
            Path(os.getenv('M1_EMBEDDING_DIRECTORY',str(ROOT/'tmp/m1-models/bge-m3'))),
            Path(os.getenv('M1_PRICE_SNAPSHOT',str(ROOT/'config/m1-pricing.json'))),
            os.getenv('M1_MOCK_SCENARIO','happy'),
            os.getenv('CRACKRAG_RELEASE_MANIFEST',''),os.getenv('CRACKRAG_RELEASE_ROOT',str(ROOT)),
            os.getenv('CRACKRAG_LIVE_SESSION',''),os.getenv('CRACKRAG_LIVE_CONTROL','/state/live-control.json'))
        if result.provider not in ('mock','deepseek') or result.embedding_mode not in ('bge-m3','fixture'):
            raise ValueError('INVALID_PROVIDER_CONFIGURATION')
        if len(result.internal_token)<16:
            raise ValueError('INVALID_SERVICE_TOKEN')
        if result.provider=='deepseek' and (result.embedding_mode!='bge-m3' or result.mock_scenario!='happy'):
            raise ValueError('LIVE_REQUIRES_REAL_EMBEDDING_AND_NO_FAULT_INJECTION')
        return result
