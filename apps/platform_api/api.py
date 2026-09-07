from ninja import NinjaAPI

api = NinjaAPI(version="1", urls_namespace="v1")


@api.get("/health")
def health(request):
    return {"status": "ok", "version": "1"}
