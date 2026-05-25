from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden
from django.db.models import Count
from django.contrib import messages
from .forms import TicketForm
from .models import Ticket, EmployeeProfile

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

    my_tickets = Ticket.objects.filter(requester=request.user).order_by('-created_at')
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

    if view_mode == 'history':
        history_tickets = Ticket.objects.filter(department=profile.department, approval_status__in=['APPROVED', 'REJECTED']).order_by('-updated_at')
        return render(request, 'tickets/head_dashboard.html', {'history_tickets': history_tickets, 'view_mode': view_mode})
    else:
        pending_approvals = Ticket.objects.filter(department=profile.department, approval_status='PENDING').order_by('-created_at')
        return render(request, 'tickets/head_dashboard.html', {'pending_approvals': pending_approvals, 'view_mode': view_mode})


# ==========================================
# PAGE 3: THE IT HEAD PAGE (Dispatcher)
# ==========================================

@login_required
def it_head_dashboard(request):
    try:
        profile = request.user.employeeprofile
        if not (profile.department == 'IT' and profile.is_department_head) and not request.user.is_superuser:
            return HttpResponseForbidden("Access Denied: IT Head only.")
    except:
        if not request.user.is_superuser:
            return HttpResponseForbidden("Access Denied.")

    view_mode = request.GET.get('view', 'active')

    if request.method == 'POST':
        ticket_id = request.POST.get('ticket_id')
        tech_id = request.POST.get('tech_id')
        
        # --- 1. NEW: Grab the priority from the IT Head's form ---
        new_priority = request.POST.get('priority') 
        
        ticket = get_object_or_404(Ticket, id=ticket_id)
        tech = get_object_or_404(User, id=tech_id)
        
        # --- 2. Update all the dispatch values ---
        ticket.assigned_to = tech
        ticket.priority = new_priority  # Apply the IT Head's priority
        ticket.status = 'IN_PROGRESS'
        ticket.is_escalated = False 
        
        ticket.save()
        messages.success(request, f"Ticket assigned to {tech.username} with {new_priority} priority.")
        return redirect('it_head_dashboard')

    if view_mode == 'history':
        # Show all tickets that have been assigned out or are completely finished
        history_tickets = Ticket.objects.filter(approval_status__in=['APPROVED', 'NOT_REQUIRED']).exclude(status='OPEN').order_by('-updated_at')
        return render(request, 'tickets/it_head_dashboard.html', {'history_tickets': history_tickets, 'view_mode': view_mode})
    else:
        unassigned_tickets = Ticket.objects.filter(approval_status__in=['APPROVED', 'NOT_REQUIRED'], status='OPEN').order_by('-created_at')
        it_team = User.objects.filter(employeeprofile__department='IT', employeeprofile__is_department_head=False)
        return render(request, 'tickets/it_head_dashboard.html', {'unassigned_tickets': unassigned_tickets, 'it_team': it_team, 'view_mode': view_mode})


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

    if view_mode == 'history':
        # Show only tickets that THIS specific staff member successfully resolved
        history_tickets = Ticket.objects.filter(assigned_to=request.user, status__in=['RESOLVED', 'CLOSED']).order_by('-updated_at')
        return render(request, 'tickets/it_staff_dashboard.html', {'history_tickets': history_tickets, 'view_mode': view_mode})
    else:
        my_tasks = Ticket.objects.filter(assigned_to=request.user, status='IN_PROGRESS').order_by('-created_at')
        return render(request, 'tickets/it_staff_dashboard.html', {'my_tasks': my_tasks, 'view_mode': view_mode})