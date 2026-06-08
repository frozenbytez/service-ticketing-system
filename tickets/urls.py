from django.urls import path
from . import views

urlpatterns = [
    # THE TRAFFIC COP
    path('', views.dashboard_redirect, name='dashboard_redirect'),
    
    # PAGE 1: Employee
    path('portal/', views.clinic_portal, name='clinic_portal'),
    path('new/', views.create_ticket, name='create_ticket'),
    
    # PAGE 2: Dept Head
    path('head-dashboard/', views.head_dashboard, name='head_dashboard'),
    
    # PAGE 3: IT Head (Dispatcher)
    path('it-head/', views.it_head_dashboard, name='it_head_dashboard'),
    
    # PAGE 4: IT Staff (Resolver)
    path('it-staff/', views.it_staff_dashboard, name='it_staff_dashboard'),

    path('sse/ticket-updates/', views.ticket_updates_sse, name='ticket_updates_sse'),

    path('dashboard/employee/', views.employee_dashboard, name='employee_dashboard'),
    path('dashboard/head/home/', views.head_dashboard_home, name='head_dashboard_home'),
    path('dashboard/it-head/home/', views.it_head_dashboard_home, name='it_head_dashboard_home'),
    path('dashboard/it-staff/home/', views.it_staff_dashboard_home, name='it_staff_dashboard_home'),

     # ── Notifications API ──────────────────────────────────────
    path('notifications/', views.notifications_api, name='notifications_api'),
    path('notifications/<int:notif_id>/click/', views.read_and_redirect_notification, name='notification_click'),
    path('notifications/inbox/', views.notifications_inbox, name='notifications_inbox'),
]