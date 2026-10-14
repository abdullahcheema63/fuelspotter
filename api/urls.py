from django.urls import path

from api.views import RouteMapView, RouteView

urlpatterns = [
    path('route/', RouteView.as_view(), name='route'),
    path('route/map/<uuid:public_id>/', RouteMapView.as_view(), name='route-map'),
]
