from django.contrib import admin
from .models import Ticket, EmployeeProfile

# Register the Employee Profile the basic way
admin.site.register(EmployeeProfile)

# Register the Ticket model using the decorator to apply the custom layout
@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    # What columns show up in the table
    list_display = ('title', 'department', 'status', 'priority', 'assigned_to', 'created_at')
    
    # The filter sidebar on the right
    list_filter = ('status', 'priority', 'department', 'assigned_to')
    
    # The search bar at the top (Updated to match our new database fields!)
    # Note: 'requester__username' lets us search inside the connected User account
    search_fields = ('title', 'client_name', 'client_email', 'requester__username')