from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DefaultUserAdmin
from django.contrib.auth.models import User
from django.contrib.auth.forms import AdminUserCreationForm  # <-- 1. Import the new Admin-specific form
from .models import Ticket, EmployeeProfile

# ==========================================
# CUSTOM USER CREATION (Forces Name & Email)
# ==========================================

# 2. Inherit from the new Admin form instead of the standard one
class CustomUserCreationForm(AdminUserCreationForm):
    class Meta(AdminUserCreationForm.Meta):
        model = User
        # 3. Add our custom fields to whatever Django requires by default (fixes the crash!)
        fields = AdminUserCreationForm.Meta.fields + ('first_name', 'last_name', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Django makes these optional by default, so we force them to be required!
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        self.fields['email'].required = True

# We inject that form into the Admin panel
class CustomUserAdmin(DefaultUserAdmin):
    add_form = CustomUserCreationForm
    
    # We add a new section to the "Add User" screen
    add_fieldsets = DefaultUserAdmin.add_fieldsets + (
        ('Personal Info (Required for Ticketing)', {
            'fields': ('first_name', 'last_name', 'email')
        }),
    )

# Unregister the boring default User admin, and register our upgraded one
admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)


# ==========================================
# SYSTEM MODELS (Tickets & Profiles)
# ==========================================

# Register the Employee Profile the basic way
admin.site.register(EmployeeProfile)

# Register the Ticket model using the decorator to apply the custom layout
@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    # What columns show up in the table
    list_display = ('title', 'department', 'status', 'priority', 'assigned_to', 'created_at')
    
    # The filter sidebar on the right
    list_filter = ('status', 'priority', 'department', 'assigned_to')
    
    # The search bar at the top
    search_fields = ('title', 'client_name', 'client_email', 'requester__username')