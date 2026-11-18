from django.urls import path

from demo import views

urlpatterns = [
    path('', views.home, name='home'),
    path('episode/<int:number>', views.episode, name='episode'),
    path('stats', views.stats, name='stats'),
    path('visits', views.visits, name='visits'),
    path('session', views.session_count, name='session'),
    path('health', views.health, name='health'),
]
