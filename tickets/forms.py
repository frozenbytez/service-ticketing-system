from django import forms
from .models import Ticket

class TicketForm(forms.ModelForm):
    class Meta:
        model = Ticket
        fields = ['client_name', 'client_email', 'title', 'description', 'priority', 'needs_head_approval']