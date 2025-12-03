from django.contrib import admin
from django.urls import path
from uploads.views import upload_file
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', upload_file, name='upload'),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)