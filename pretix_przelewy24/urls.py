from django.urls import path
from pretix.multidomain import event_path

from . import views

event_patterns = [
    path(
        "_przelewy24/return/<str:order>/<str:hash>/<int:payment>/",
        views.ReturnView.as_view(),
        name="return",
    ),
    event_path(
        "_przelewy24/callback/<str:order>/<str:hash>/<int:payment>/",
        views.CallbackView.as_view(),
        name="callback",
        require_live=False,
    ),
    event_path(
        "_przelewy24/refundcallback/<str:order>/<str:hash>/<int:refund>/",
        views.RefundCallbackView.as_view(),
        name="refundcallback",
        require_live=False,
    ),
]
