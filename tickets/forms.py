from django import forms
from .models import Ticket

class TicketForm(forms.ModelForm):
    class Meta:
        model = Ticket
        # We removed 'priority' from this list entirely
        fields = ['title', 'description', 'client_name']