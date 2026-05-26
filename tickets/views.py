from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden
from django.db.models import Count
from django.contrib import messages
from .forms import TicketForm
from .models import Ticket, EmployeeProfile
import time
import json
from django.http import StreamingHttpResponse
from .models import Ticket
from django.db.models import Q
from django.db.models import Avg, Count
# ==========================================
# THE TRAFFIC COP (Login Router)
# ==========================================

@login_required
def dashboard_redirect(request):
    """Routes users to 1 of 4 specific dashboards based on their exact role."""
    if request.user.is_superuser and not hasattr(request.user, 'employeeprofile'):
        return redirect('it_head_dashboard')
        
    if hasattr(request.user, 'employeeprofile'):
        profile = request.user.employeeprofile
        if profile.department == 'IT':
            if profile.is_department_head:
                return redirect('it_head_dashboard') 
            else:
                return redirect('it_staff_dashboard') 
        if profile.is_department_head:
            return redirect('head_dashboard') 
            
    return redirect('clinic_portal')


# ==========================================
# PAGE 1: THE EMPLOYEE PAGE (Requester)
# ==========================================

@login_required
def clinic_portal(request):
    if request.method == 'POST' and 'submit_feedback' in request.POST:
        ticket_id = request.POST.get('ticket_id')
        ticket = get_object_or_404(Ticket, id=ticket_id, requester=request.user)
        ticket.client_feedback_rating = request.POST.get('rating')
        ticket.client_feedback_notes = request.POST.get('feedback_notes')
        ticket.status = 'CLOSED' 
        ticket.save()
        messages.success(request, "Thank you for your feedback! The ticket is now closed.")
        return redirect('clinic_portal')

    # 1. Grab the search and filter terms from the URL
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status_filter', '')

    # 2. Get base queryset (only tickets requested by this specific user)
    my_tickets = Ticket.objects.filter(requester=request.user).order_by('-created_at')

    # 3. Apply Text Search
    if search_query:
        my_tickets = my_tickets.filter(
            Q(title__icontains=search_query) |
            Q(ticket_number__icontains=search_query) |
            Q(description__icontains=search_query)
        )

    # 4. Apply Status Dropdown Filter
    if status_filter:
        my_tickets = my_tickets.filter(status=status_filter)

    return render(request, 'tickets/clinic_portal.html', {'my_tickets': my_tickets})

@login_required
def create_ticket(request):
    if request.method == 'POST':
        form = TicketForm(request.POST)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.requester = request.user 
            ticket.department = request.user.employeeprofile.department
            
            # 1. We default the priority to 'MEDIUM' (or 'LOW') for all new tickets
            ticket.priority = 'MEDIUM'
            
            # 2. Every ticket now requires Dept Head approval by default
            ticket.needs_head_approval = True
            ticket.approval_status = 'PENDING'
                
            ticket.save()
            messages.success(request, "Request submitted successfully!")
            return redirect('clinic_portal')
    else:
        form = TicketForm(initial={
            'client_name': request.user.get_full_name() or request.user.username,
        })
        
    return render(request, 'tickets/create_ticket.html', {'form': form})


# ==========================================
# PAGE 2: THE DEPT HEAD PAGE (Approver)
# ==========================================

@login_required
def head_dashboard(request):
    try:
        profile = request.user.employeeprofile
        if not profile.is_department_head or profile.department == 'IT':
            return HttpResponseForbidden("Access Denied: Non-IT Department Heads only.")
    except:
        return HttpResponseForbidden("Access Denied.")

    view_mode = request.GET.get('view', 'active')
    
    # 1. Grab the search and filter terms from the URL
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status_filter', '')

    if request.method == 'POST' and 'handle_approval' in request.POST:
        ticket_id = request.POST.get('ticket_id')
        action = request.POST.get('action') 
        ticket = get_object_or_404(Ticket, id=ticket_id, department=profile.department)
        
        if action == 'APPROVE':
            ticket.approval_status = 'APPROVED'
            messages.success(request, f"Ticket '{ticket.title}' approved and sent to IT.")
        elif action == 'REJECT':
            ticket.approval_status = 'REJECTED'
            ticket.status = 'CLOSED' 
            messages.error(request, f"Ticket '{ticket.title}' rejected.")
        ticket.save()
        return redirect('head_dashboard')

    # 2. Get base querysets (THE FIX IS HERE)
    pending_approvals = Ticket.objects.filter(department=profile.department, approval_status='PENDING').order_by('-created_at')
    
    # Grab ALL tickets for the department that are NOT in the pending queue
    history_tickets = Ticket.objects.filter(department=profile.department).exclude(approval_status='PENDING').order_by('-updated_at')

    # 3. Apply Text Search
    if search_query:
        pending_approvals = pending_approvals.filter(
            Q(title__icontains=search_query) |
            Q(ticket_number__icontains=search_query) |
            Q(client_name__icontains=search_query) |
            Q(description__icontains=search_query)
        )
        history_tickets = history_tickets.filter(
            Q(title__icontains=search_query) |
            Q(ticket_number__icontains=search_query) |
            Q(client_name__icontains=search_query) |
            Q(description__icontains=search_query)
        )

    # 4. Apply Status Filter
    if status_filter:
        if view_mode == 'history':
            history_tickets = history_tickets.filter(status=status_filter)
        else:
            pending_approvals = pending_approvals.filter(status=status_filter)

    # 5. Render
    if view_mode == 'history':
        return render(request, 'tickets/head_dashboard.html', {'history_tickets': history_tickets, 'view_mode': view_mode})
    else:
        return render(request, 'tickets/head_dashboard.html', {'pending_approvals': pending_approvals, 'view_mode': view_mode})


# ==========================================
# PAGE 3: THE IT HEAD PAGE (Dispatcher)
# ==========================================

@login_required
def it_head_dashboard(request):
    # ... your existing POST logic for dispatching tickets goes here ...

    view_mode = request.GET.get('view', 'active')
    
    # 1. Grab the search and filter terms from the URL
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status_filter', '')

    # 2. Get your base querysets
    unassigned_tickets = Ticket.objects.filter(status='OPEN', approval_status__in=['APPROVED', 'NOT_REQUIRED']).order_by('-created_at')
    history_tickets = Ticket.objects.exclude(status='OPEN').order_by('-updated_at')

    # 3. Apply the Text Search (if the user typed something)
    if search_query:
        # Search Title OR Ticket Number OR Client Name
        unassigned_tickets = unassigned_tickets.filter(
            Q(title__icontains=search_query) |
            Q(ticket_number__icontains=search_query) |
            Q(client_name__icontains=search_query) |
            Q(description__icontains=search_query)
        )
        
        history_tickets = history_tickets.filter(
            Q(title__icontains=search_query) |
            Q(ticket_number__icontains=search_query) |
            Q(client_name__icontains=search_query) |
            Q(assigned_to__username__icontains=search_query)
        )

    # 4. Apply the Status Dropdown Filter (if the user selected one)
    if status_filter:
        if view_mode == 'history':
            history_tickets = history_tickets.filter(status=status_filter)
        else:
            unassigned_tickets = unassigned_tickets.filter(status=status_filter)

    # ... get your it_team list ...
    it_team = User.objects.filter(employeeprofile__department='IT', employeeprofile__is_department_head=False)

    context = {
        'unassigned_tickets': unassigned_tickets,
        'history_tickets': history_tickets,
        'it_team': it_team,
        'view_mode': view_mode,
    }
    return render(request, 'tickets/it_head_dashboard.html', context)


# ==========================================
# PAGE 4: THE IT STAFF PAGE (Resolver)
# ==========================================

@login_required
def it_staff_dashboard(request):
    try:
        profile = request.user.employeeprofile
        if profile.department != 'IT' or profile.is_department_head:
            return HttpResponseForbidden("Access Denied: IT Staff only.")
    except:
        return HttpResponseForbidden("Access Denied.")

    view_mode = request.GET.get('view', 'active')

    if request.method == 'POST':
        ticket_id = request.POST.get('ticket_id')
        action = request.POST.get('action')
        ticket = get_object_or_404(Ticket, id=ticket_id, assigned_to=request.user)

        if action == 'ESCALATE':
            ticket.is_escalated = True
            ticket.assigned_to = None  
            ticket.status = 'OPEN'     
            messages.warning(request, f"Ticket {ticket.title} escalated back to IT Head.")
        elif action == 'RESOLVE':
            ticket.status = 'RESOLVED'
            ticket.resolution_notes = request.POST.get('resolution_notes', 'Resolved by IT.')
            messages.success(request, f"Ticket {ticket.title} marked as Resolved.")
            
        ticket.save()
        return redirect('it_staff_dashboard')

    # CALCULATE PERFORMANCE STATS
    my_stats = Ticket.objects.filter(
        assigned_to=request.user, 
        status='CLOSED', 
        client_feedback_rating__isnull=False
    ).aggregate(
        total_closed=Count('id'),
        average_rating=Avg('client_feedback_rating')
    )

    if view_mode == 'history':
        history_tickets = Ticket.objects.filter(assigned_to=request.user, status__in=['RESOLVED', 'CLOSED']).order_by('-updated_at')
        return render(request, 'tickets/it_staff_dashboard.html', {'history_tickets': history_tickets, 'view_mode': view_mode, 'my_stats': my_stats})
    else:
        my_tasks = Ticket.objects.filter(assigned_to=request.user, status='IN_PROGRESS').order_by('-created_at')
        return render(request, 'tickets/it_staff_dashboard.html', {'my_tasks': my_tasks, 'view_mode': view_mode, 'my_stats': my_stats})
        

def ticket_updates_sse(request):
    """
    Server-Sent Events endpoint.
    Pushes a message to the frontend if a ticket has been updated.
    """
    def event_stream():
        latest_ticket = Ticket.objects.order_by('-updated_at').first()
        last_update = latest_ticket.updated_at if latest_ticket else None

        try:
            while True:
                time.sleep(3)
                
                current_ticket = Ticket.objects.order_by('-updated_at').first()
                current_update = current_ticket.updated_at if current_ticket else None

                if current_update != last_update:
                    yield f"data: {json.dumps({'refresh_required': True})}\n\n"
                    last_update = current_update
                else:
                    yield f"data: {json.dumps({'refresh_required': False})}\n\n"
                    
        except GeneratorExit:
            # The browser closed the connection. Exit the loop cleanly.
            return

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response