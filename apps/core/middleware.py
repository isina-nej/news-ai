import uuid


class CorrelationIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        cid = request.headers.get("X-Correlation-ID", uuid.uuid4().hex[:16])
        request.correlation_id = cid
        response = self.get_response(request)
        response["X-Correlation-ID"] = cid
        return response
