import grpc
from crackrag.v1 import runtime_pb2 as pb,runtime_pb2_grpc as rpc
from .config import Settings

settings=Settings.load()
target=settings.address.replace('0.0.0.0','127.0.0.1')
with grpc.insecure_channel(target) as channel:
    result=rpc.AIRuntimeStub(channel).Health(pb.Empty(),metadata=[('authorization','Bearer '+settings.internal_token)],timeout=3)
    if result.status!='ok' or result.protocol_version!='crackrag.v1':raise SystemExit(1)
