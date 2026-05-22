from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView  # 1. Import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('tickets/', include('tickets.urls')),
    path('accounts/', include('django.contrib.auth.urls')),
    
    # 2. Add this line to catch the empty homepage and push them to the tickets app!
    path('', RedirectView.as_view(url='/tickets/')), 
]