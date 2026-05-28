from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden, StreamingHttpResponse
from django.db.models import Count, Avg, Q
from django.contrib import messages
from .forms import TicketForm
from .models import Ticket, EmployeeProfile
import time
import json
from datetime import datetime


# ──────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────

def _generate_ticket_number():
    """Generate a sequential ticket number like TKT-202506-0001."""
    year_month = datetime.now().strftime('%Y%m')
    prefix = f'TKT-{year_month}-'
    last = (
        Ticket.objects
        .filter(ticket_number__startswith=prefix)
        .order_by('-ticket_number')
        .first()
    )
    if last and last.ticket_number:
        try:
            num = int(last.ticket_number.split('-')[-1]) + 1
        except (ValueError, IndexError):
            num = Ticket.objects.filter(ticket_number__isnull=False).count() + 1
    else:
        num = Ticket.objects.filter(ticket_number__isnull=False).count() + 1
    return f'{prefix}{num:04d}'


def _escalation_count():
    """Count of escalated tickets sitting in the open queue."""
    return Ticket.objects.filter(status='OPEN', is_escalated=True).count()


# ──────────────────────────────────────────────────────────────
# TRAFFIC COP (Login Router)
# ──────────────────────────────────────────────────────────────

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
                return redirect('it_staff_dashboard')   # Tier 1 & Tier 2 share dashboard
        if profile.is_department_head:
            return redirect('head_dashboard')

    return redirect('clinic_portal')


# ──────────────────────────────────────────────────────────────
# PAGE 1: CLINIC PORTAL (Requester)
# ──────────────────────────────────────────────────────────────

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

    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status_filter', '')

    my_tickets = Ticket.objects.filter(requester=request.user).order_by('-created_at')

    if search_query:
        my_tickets = my_tickets.filter(
            Q(title__icontains=search_query) |
            Q(ticket_number__icontains=search_query) |
            Q(description__icontains=search_query)
        )

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
            ticket.priority = 'MEDIUM'
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


# ──────────────────────────────────────────────────────────────
# PAGE 2: DEPT HEAD DASHBOARD (Approver)
# ──────────────────────────────────────────────────────────────

@login_required
def head_dashboard(request):
    try:
        profile = request.user.employeeprofile
        if not profile.is_department_head or profile.department == 'IT':
            return HttpResponseForbidden("Access Denied: Non-IT Department Heads only.")
    except Exception:
        return HttpResponseForbidden("Access Denied.")

    view_mode = request.GET.get('view', 'active')
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

    pending_approvals = (
        Ticket.objects
        .filter(department=profile.department, approval_status='PENDING')
        .order_by('-created_at')
    )
    history_tickets = (
        Ticket.objects
        .filter(department=profile.department)
        .exclude(approval_status='PENDING')
        .order_by('-updated_at')
    )

    if search_query:
        q = Q(title__icontains=search_query) | Q(ticket_number__icontains=search_query) | \
            Q(client_name__icontains=search_query) | Q(description__icontains=search_query)
        pending_approvals = pending_approvals.filter(q)
        history_tickets = history_tickets.filter(q)

    if status_filter:
        if view_mode == 'history':
            history_tickets = history_tickets.filter(status=status_filter)
        else:
            pending_approvals = pending_approvals.filter(status=status_filter)

    ctx = {'view_mode': view_mode}
    if view_mode == 'history':
        ctx['history_tickets'] = history_tickets
    else:
        ctx['pending_approvals'] = pending_approvals

    return render(request, 'tickets/head_dashboard.html', ctx)


# ──────────────────────────────────────────────────────────────
# PAGE 3: IT HEAD DASHBOARD (Dispatcher)
# ──────────────────────────────────────────────────────────────

@login_required
def it_head_dashboard(request):
    view_mode = request.GET.get('view', 'active')
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status_filter', '')

    # ── DISPATCH POST ───────────────────────────────────────────
    if request.method == 'POST':
        ticket_id = request.POST.get('ticket_id')
        tech_id = request.POST.get('tech_id')
        priority = request.POST.get('priority', 'MEDIUM')
        ticket = get_object_or_404(Ticket, id=ticket_id)
        tech = get_object_or_404(User, id=tech_id)

        ticket.assigned_to = tech
        ticket.priority = priority
        ticket.status = 'IN_PROGRESS'

        # Generate ticket number on first dispatch
        if not ticket.ticket_number:
            ticket.ticket_number = _generate_ticket_number()

        ticket.save()
        tech_name = tech.get_full_name() or tech.username
        messages.success(
            request,
            f"Ticket #{ticket.ticket_number} dispatched to {tech_name}!"
        )
        return redirect('it_head_dashboard')

    # ── QUERYSETS ───────────────────────────────────────────────
    unassigned_tickets = (
        Ticket.objects
        .filter(status='OPEN', approval_status__in=['APPROVED', 'NOT_REQUIRED'])
        .order_by('-is_escalated', '-created_at')   # escalated first
    )
    history_tickets = (
        Ticket.objects
        .exclude(status='OPEN')
        .order_by('-updated_at')
    )

    if search_query:
        unassigned_tickets = unassigned_tickets.filter(
            Q(title__icontains=search_query) | Q(ticket_number__icontains=search_query) |
            Q(client_name__icontains=search_query) | Q(description__icontains=search_query)
        )
        history_tickets = history_tickets.filter(
            Q(title__icontains=search_query) | Q(ticket_number__icontains=search_query) |
            Q(client_name__icontains=search_query) |
            Q(assigned_to__username__icontains=search_query)
        )

    if status_filter:
        if view_mode == 'history':
            history_tickets = history_tickets.filter(status=status_filter)
        else:
            unassigned_tickets = unassigned_tickets.filter(status=status_filter)

    # IT team with tier info
    it_team = (
        User.objects
        .filter(employeeprofile__department='IT', employeeprofile__is_department_head=False)
        .select_related('employeeprofile')
    )

    context = {
        'unassigned_tickets': unassigned_tickets,
        'history_tickets': history_tickets,
        'it_team': it_team,
        'view_mode': view_mode,
        'escalation_count': _escalation_count(),
    }
    return render(request, 'tickets/it_head_dashboard.html', context)


# ──────────────────────────────────────────────────────────────
# PAGE 4: IT STAFF DASHBOARD (Resolver — Tier 1 & Tier 2)
# ──────────────────────────────────────────────────────────────

@login_required
def it_staff_dashboard(request):
    try:
        profile = request.user.employeeprofile
        if profile.department != 'IT' or profile.is_department_head:
            return HttpResponseForbidden("Access Denied: IT Staff only.")
    except Exception:
        return HttpResponseForbidden("Access Denied.")

    view_mode = request.GET.get('view', 'active')
    priority_filter = request.GET.get('priority_filter', '')

    if request.method == 'POST':
        ticket_id = request.POST.get('ticket_id')
        action = request.POST.get('action')
        ticket = get_object_or_404(Ticket, id=ticket_id, assigned_to=request.user)

        if action == 'ESCALATE':
            ticket.is_escalated = True
            ticket.escalated_from_tier = profile.it_tier   # track which tier escalated
            ticket.assigned_to = None
            ticket.status = 'OPEN'
            tier_label = profile.get_it_tier_display()
            messages.warning(
                request,
                f"Ticket '{ticket.title}' escalated from {tier_label} back to IT Head."
            )
        elif action == 'RESOLVE':
            ticket.status = 'RESOLVED'
            ticket.resolution_notes = request.POST.get('resolution_notes', 'Resolved by IT.')
            messages.success(request, f"Ticket '{ticket.title}' marked as Resolved.")

        ticket.save()
        return redirect('it_staff_dashboard')

    # ── PERFORMANCE STATS ────────────────────────────────────────
    my_stats = Ticket.objects.filter(
        assigned_to=request.user,
        status='CLOSED',
        client_feedback_rating__isnull=False
    ).aggregate(
        total_closed=Count('id'),
        average_rating=Avg('client_feedback_rating')
    )

    # ── RATING DETAIL DATA FOR MODAL ─────────────────────────────
    rating_tickets_qs = (
        Ticket.objects
        .filter(assigned_to=request.user, status='CLOSED', client_feedback_rating__isnull=False)
        .order_by('-updated_at')
    )

    rating_data = []
    for t in rating_tickets_qs:
        rating_data.append({
            'ticket_number': t.ticket_number or '—',
            'title': t.title,
            'client_name': t.client_name,
            'rating': t.client_feedback_rating,
            'notes': t.client_feedback_notes or '',
            'date': t.updated_at.strftime('%b %d, %Y') if t.updated_at else '',
        })

    rating_breakdown = {}
    for i in range(1, 6):
        rating_breakdown[i] = rating_tickets_qs.filter(client_feedback_rating=i).count()

    # Notification count: escalated tickets newly assigned to this Tier 2 user
    new_escalation_count = 0
    if profile.it_tier == 'TIER_2':
        new_escalation_count = Ticket.objects.filter(
            assigned_to=request.user,
            status='IN_PROGRESS',
            is_escalated=True
        ).count()

    base_ctx = {
        'my_stats': my_stats,
        'view_mode': view_mode,
        'profile': profile,
        'rating_data_json': json.dumps(rating_data),
        'rating_breakdown_json': json.dumps(rating_breakdown),
        'new_escalation_count': new_escalation_count,
        'priority_filter': priority_filter,
    }

    if view_mode == 'history':
        history_tickets = (
            Ticket.objects
            .filter(assigned_to=request.user, status__in=['RESOLVED', 'CLOSED'])
            .order_by('-updated_at')
        )
        return render(
            request, 'tickets/it_staff_dashboard.html',
            {**base_ctx, 'history_tickets': history_tickets}
        )
    else:
        my_tasks = Ticket.objects.filter(
            assigned_to=request.user, status='IN_PROGRESS'
        ).order_by('-created_at')

        if priority_filter:
            my_tasks = my_tasks.filter(priority=priority_filter)

        return render(
            request, 'tickets/it_staff_dashboard.html',
            {**base_ctx, 'my_tasks': my_tasks}
        )


# ──────────────────────────────────────────────────────────────
# SSE: REAL-TIME TICKET UPDATE STREAM
# ──────────────────────────────────────────────────────────────

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
            return

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response