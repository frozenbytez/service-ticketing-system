from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden, StreamingHttpResponse
from django.db.models import Count, Avg, Q
from django.contrib import messages
from .forms import TicketForm
from .models import Ticket, EmployeeProfile, DEPARTMENT_CHOICES
from django.utils import timezone
from datetime import timedelta, datetime, date
import calendar
import time
import json
from .models import RecurringTask

# ──────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────

def apply_time_filter(queryset, time_filter):
    now = timezone.now()
    if time_filter == 'daily':
        return queryset.filter(created_at__date=now.date())
    elif time_filter == 'weekly':
        start_of_week = now - timedelta(days=now.weekday())
        return queryset.filter(created_at__date__gte=start_of_week.date())
    elif time_filter == 'monthly':
        return queryset.filter(created_at__year=now.year, created_at__month=now.month)
    elif time_filter == 'yearly':
        return queryset.filter(created_at__year=now.year)
    return queryset


def _generate_ticket_number():
    year_month = datetime.now().strftime('%Y%m')
    prefix = f'TKT-{year_month}-'
    last = Ticket.objects.filter(ticket_number__startswith=prefix).order_by('-ticket_number').first()
    if last and last.ticket_number:
        try:
            num = int(last.ticket_number.split('-')[-1]) + 1
        except (ValueError, IndexError):
            num = Ticket.objects.filter(ticket_number__isnull=False).count() + 1
    else:
        num = Ticket.objects.filter(ticket_number__isnull=False).count() + 1
    return f'{prefix}{num:04d}'


def _escalation_count():
    return Ticket.objects.filter(status='OPEN', is_escalated=True).count()


def _monthly_trend(base_qs, months=6):
    """Returns (labels, counts) for the last N months."""
    now = timezone.now()
    labels, counts = [], []
    for i in range(months - 1, -1, -1):
        d = now - timedelta(days=30 * i)
        cnt = base_qs.filter(created_at__year=d.year, created_at__month=d.month).count()
        labels.append(d.strftime('%b'))
        counts.append(cnt)
    return labels, counts


def check_and_generate_recurring_tasks():
    """Checks active recurring tasks and generates standard tickets if they are due."""
    today = date.today()
    active_tasks = RecurringTask.objects.filter(is_active=True)

    for task in active_tasks:
        should_generate = False

        if task.recurrence_type == 'DAILY':
            if not task.last_generated or task.last_generated < today:
                should_generate = True
        elif task.recurrence_type == 'WEEKLY':
            start_of_week = today - timedelta(days=today.weekday())
            if not task.last_generated or task.last_generated < start_of_week:
                should_generate = True
        elif task.recurrence_type == 'MONTHLY':
            start_of_month = today.replace(day=1)
            if not task.last_generated or task.last_generated < start_of_month:
                should_generate = True
        elif task.recurrence_type == 'QUARTERLY':
            if today.month in [3, 6, 9, 12]:
                last_day = calendar.monthrange(today.year, today.month)[1]
                start_of_last_week = today.replace(day=last_day) - timedelta(days=7)
                if today >= start_of_last_week:
                    if not task.last_generated or task.last_generated < start_of_last_week:
                        should_generate = True
        elif task.recurrence_type == 'CUSTOM':
            if task.custom_date_start and task.custom_date_end:
                if task.custom_date_start <= today <= task.custom_date_end:
                    if not task.last_generated or task.last_generated < task.custom_date_start:
                        should_generate = True

        if should_generate:
            notes = f"Auto-generated for {task.get_recurrence_type_display()} maintenance schedule."
            if task.recurrence_type == 'CUSTOM' and task.custom_date_start and task.custom_date_end:
                notes += f" (Must be completed between {task.custom_date_start.strftime('%b %d')} and {task.custom_date_end.strftime('%b %d, %Y')})"

            ticket = Ticket.objects.create(
                title=f"[PM] {task.title}",
                description=task.description,
                department='IT',
                requester=task.created_by,
                client_name="System Scheduled Maintenance",
                assigned_to=task.assigned_to,
                priority=task.priority,
                status='IN_PROGRESS' if task.assigned_to else 'OPEN',
                approval_status='APPROVED',
                needs_head_approval=False,
                is_preventive_maintenance=True,
                dispatch_notes=notes
            )
            ticket.ticket_number = _generate_ticket_number()
            ticket.save()

            task.last_generated = today
            task.save()

# ──────────────────────────────────────────────────────────────
# TRAFFIC COP & HOME DASHBOARDS
# ──────────────────────────────────────────────────────────────

@login_required
def dashboard_redirect(request):
    if request.user.is_superuser and not hasattr(request.user, 'employeeprofile'):
        return redirect('it_head_dashboard_home')

    if hasattr(request.user, 'employeeprofile'):
        profile = request.user.employeeprofile
        if profile.department == 'IT':
            if profile.is_department_head:
                return redirect('it_head_dashboard_home')
            else:
                return redirect('it_staff_dashboard_home')
        if profile.is_department_head:
            return redirect('head_dashboard_home')
    return redirect('employee_dashboard')


@login_required
def employee_dashboard(request):
    time_filter = request.GET.get('time_filter', 'all')
    tickets = apply_time_filter(Ticket.objects.filter(requester=request.user), time_filter)

    # ── Core Stats ──
    total_sent     = tickets.count()
    total_approved = tickets.filter(approval_status='APPROVED').count()
    total_declined = tickets.filter(approval_status='REJECTED').count()
    total_pending  = tickets.filter(approval_status='PENDING').count()
    total_fixed    = tickets.filter(status__in=['RESOLVED', 'CLOSED']).count()
    total_feedbacks = tickets.filter(client_feedback_rating__isnull=False).count()

    # ── Priority Breakdown Chart ──
    priority_data = [
        tickets.filter(priority='LOW').count(),
        tickets.filter(priority='MEDIUM').count(),
        tickets.filter(priority='HIGH').count(),
        tickets.filter(priority='URGENT').count(),
    ]

    # ── Status Breakdown Chart ──
    status_data = [
        tickets.filter(status='OPEN').count(),
        tickets.filter(status='IN_PROGRESS').count(),
        tickets.filter(status='RESOLVED').count(),
        tickets.filter(status='CLOSED').count(),
    ]

    # ── 6-Month Trend ──
    all_my_tickets = Ticket.objects.filter(requester=request.user)
    monthly_labels, monthly_counts = _monthly_trend(all_my_tickets)

    context = {
        'time_filter': time_filter,
        'total_sent':      total_sent,
        'total_approved':  total_approved,
        'total_declined':  total_declined,
        'total_fixed':     total_fixed,
        'total_feedbacks': total_feedbacks,
        # Chart JSON
        'status_chart_data':     json.dumps([total_approved, total_declined, total_pending]),
        'action_chart_data':     json.dumps([total_sent, total_fixed, total_feedbacks]),
        'priority_chart_data':   json.dumps(priority_data),
        'status_breakdown_data': json.dumps(status_data),
        'monthly_labels':        json.dumps(monthly_labels),
        'monthly_counts':        json.dumps(monthly_counts),
    }
    return render(request, 'tickets/employee_dashboard.html', context)


@login_required
def head_dashboard_home(request):
    time_filter = request.GET.get('time_filter', 'all')
    dept = request.user.employeeprofile.department
    tickets = apply_time_filter(Ticket.objects.filter(department=dept), time_filter)

    # ── Core Stats ──
    total_sent     = tickets.count()
    total_approved = tickets.filter(approval_status='APPROVED').count()
    total_declined = tickets.filter(approval_status='REJECTED').count()
    total_pending  = tickets.filter(approval_status='PENDING').count()
    total_fixed    = tickets.filter(status__in=['RESOLVED', 'CLOSED']).count()
    total_feedbacks = tickets.filter(client_feedback_rating__isnull=False).count()

    # ── Priority Breakdown Chart ──
    priority_data = [
        tickets.filter(priority='LOW').count(),
        tickets.filter(priority='MEDIUM').count(),
        tickets.filter(priority='HIGH').count(),
        tickets.filter(priority='URGENT').count(),
    ]

    # ── Status Breakdown Chart ──
    status_data = [
        tickets.filter(status='OPEN').count(),
        tickets.filter(status='IN_PROGRESS').count(),
        tickets.filter(status='RESOLVED').count(),
        tickets.filter(status='CLOSED').count(),
    ]

    # ── 6-Month Trend ──
    all_dept_tickets = Ticket.objects.filter(department=dept)
    monthly_labels, monthly_counts = _monthly_trend(all_dept_tickets)

    context = {
        'time_filter': time_filter,
        'total_sent':      total_sent,
        'total_approved':  total_approved,
        'total_declined':  total_declined,
        'total_fixed':     total_fixed,
        'total_feedbacks': total_feedbacks,
        # Chart JSON
        'approval_chart_data':   json.dumps([total_approved, total_declined, total_pending]),
        'priority_chart_data':   json.dumps(priority_data),
        'status_breakdown_data': json.dumps(status_data),
        'monthly_labels':        json.dumps(monthly_labels),
        'monthly_counts':        json.dumps(monthly_counts),
    }
    return render(request, 'tickets/head_dashboard_home.html', context)


@login_required
def it_head_dashboard_home(request):
    time_filter = request.GET.get('time_filter', 'all')
    dept_filter = request.GET.get('dept_filter', 'all')

    tickets = apply_time_filter(Ticket.objects.all(), time_filter)
    if dept_filter != 'all':
        tickets = tickets.filter(department=dept_filter)

    # ── Core Stats (all were missing from original view) ──
    total_sent     = tickets.count()
    total_approved = tickets.filter(approval_status='APPROVED').count()
    total_declined = tickets.filter(approval_status='REJECTED').count()
    total_pending  = tickets.filter(approval_status='PENDING').count()
    total_assigned  = tickets.filter(assigned_to__isnull=False).count()
    total_fixed     = tickets.filter(status__in=['RESOLVED', 'CLOSED']).count()
    total_escalated = tickets.filter(is_escalated=True).count()

    # ── Department Bar Chart ──
    dept_counts = tickets.values('department').annotate(count=Count('id'))
    chart_depts       = [d['department'] for d in dept_counts]
    chart_dept_counts = [d['count']      for d in dept_counts]

    # ── Status Breakdown ──
    status_data = [
        tickets.filter(status='OPEN').count(),
        tickets.filter(status='IN_PROGRESS').count(),
        tickets.filter(status='RESOLVED').count(),
        tickets.filter(status='CLOSED').count(),
    ]

    # ── Priority Breakdown ──
    priority_data = [
        tickets.filter(priority='LOW').count(),
        tickets.filter(priority='MEDIUM').count(),
        tickets.filter(priority='HIGH').count(),
        tickets.filter(priority='URGENT').count(),
    ]

    # ── 6-Month Trend ──
    monthly_labels, monthly_counts = _monthly_trend(Ticket.objects.all())

    # ── Staff Performance ──
    staff_stats = []
    it_staffs = User.objects.filter(
        employeeprofile__department='IT',
        employeeprofile__is_department_head=False
    ).select_related('employeeprofile')

    for staff in it_staffs:
        staff_tix = apply_time_filter(
            Ticket.objects.filter(assigned_to=staff, client_feedback_rating__isnull=False),
            time_filter
        )
        agg = staff_tix.aggregate(avg_rating=Avg('client_feedback_rating'), total_feedbacks=Count('id'))
        tier = staff.employeeprofile.get_it_tier_display() if hasattr(staff, 'employeeprofile') else '—'
        staff_stats.append({
            'name':           staff.get_full_name() or staff.username,
            'tier':           tier,
            'assigned':       tickets.filter(assigned_to=staff).count(),
            'resolved':       tickets.filter(assigned_to=staff, status__in=['RESOLVED', 'CLOSED']).count(),
            'avg_rating':     round(agg['avg_rating'], 1) if agg['avg_rating'] else 0.0,
            'total_feedbacks': agg['total_feedbacks'] or 0,
        })
    staff_stats_sorted = sorted(staff_stats, key=lambda x: x['avg_rating'], reverse=True)

    # ── Staff chart arrays ──
    staff_names    = [s['name']     for s in staff_stats_sorted]
    staff_resolved = [s['resolved'] for s in staff_stats_sorted]
    staff_ratings  = [s['avg_rating'] for s in staff_stats_sorted]

    context = {
        'time_filter': time_filter,
        'dept_filter': dept_filter,
        'departments': [d[0] for d in DEPARTMENT_CHOICES],
        # Stats
        'total_sent':      total_sent,
        'total_approved':  total_approved,
        'total_declined':  total_declined,
        'total_assigned':  total_assigned,
        'total_fixed':     total_fixed,
        'total_escalated': total_escalated,
        # Table
        'staff_stats': staff_stats_sorted,
        # Chart JSON
        'chart_depts_json':       json.dumps(chart_depts),
        'chart_dept_counts_json': json.dumps(chart_dept_counts),
        'approval_chart_data':    json.dumps([total_approved, total_declined, total_pending]),
        'status_breakdown_data':  json.dumps(status_data),
        'priority_chart_data':    json.dumps(priority_data),
        'monthly_labels':         json.dumps(monthly_labels),
        'monthly_counts':         json.dumps(monthly_counts),
        'staff_names_json':       json.dumps(staff_names),
        'staff_resolved_json':    json.dumps(staff_resolved),
        'staff_ratings_json':     json.dumps(staff_ratings),
    }
    return render(request, 'tickets/it_head_dashboard_home.html', context)


@login_required
def it_staff_dashboard_home(request):
    time_filter = request.GET.get('time_filter', 'all')
    tickets = apply_time_filter(Ticket.objects.filter(assigned_to=request.user), time_filter)

    # ── Core Stats ──
    total_assigned = tickets.count()
    total_resolved = tickets.filter(status__in=['RESOLVED', 'CLOSED']).count()
    total_active   = tickets.filter(status='IN_PROGRESS').count()
    total_open     = tickets.filter(status='OPEN').count()

    # ── Most Requested Department ──
    most_dept_qs = (
        tickets.values('department')
               .annotate(count=Count('id'))
               .order_by('-count')
               .first()
    )
    dept_display = dict(DEPARTMENT_CHOICES)
    most_dept = dept_display.get(most_dept_qs['department'], '—') if most_dept_qs else '—'

    # ── Department Bar Chart ──
    dept_counts = tickets.values('department').annotate(count=Count('id'))
    chart_depts       = [d['department'] for d in dept_counts]
    chart_dept_counts = [d['count']      for d in dept_counts]

    # ── Rating Breakdown ──
    rating_data = [
        tickets.filter(client_feedback_rating=5).count(),
        tickets.filter(client_feedback_rating=4).count(),
        tickets.filter(client_feedback_rating=3).count(),
        tickets.filter(client_feedback_rating=2).count(),
        tickets.filter(client_feedback_rating=1).count(),
    ]
    max_rating_count = max(rating_data) if max(rating_data) > 0 else 1

    agg = tickets.filter(client_feedback_rating__isnull=False).aggregate(
        avg=Avg('client_feedback_rating'), count=Count('id')
    )

    # ── Priority Breakdown ──
    priority_data = [
        tickets.filter(priority='LOW').count(),
        tickets.filter(priority='MEDIUM').count(),
        tickets.filter(priority='HIGH').count(),
        tickets.filter(priority='URGENT').count(),
    ]

    # ── Status Breakdown ──
    status_data = [total_active, total_resolved, total_open]

    # ── 6-Month Trend ──
    all_my_tickets = Ticket.objects.filter(assigned_to=request.user)
    monthly_labels, monthly_counts = _monthly_trend(all_my_tickets)

    context = {
        'time_filter':    time_filter,
        'total_assigned': total_assigned,
        'total_resolved': total_resolved,
        'total_active':   total_active,
        'most_dept':      most_dept,
        'total_feedbacks': agg['count'] or 0,
        'avg_rating':     round(agg['avg'], 1) if agg['avg'] else 0.0,
        # Legacy CSS bar support
        'rating_data':    rating_data,
        'max_rating':     max_rating_count,
        # Chart JSON
        'chart_depts_json':       json.dumps(chart_depts),
        'chart_dept_counts_json': json.dumps(chart_dept_counts),
        'priority_chart_data':    json.dumps(priority_data),
        'status_chart_data':      json.dumps(status_data),
        'rating_chart_data':      json.dumps(rating_data),
        'monthly_labels':         json.dumps(monthly_labels),
        'monthly_counts':         json.dumps(monthly_counts),
    }
    return render(request, 'tickets/it_staff_dashboard_home.html', context)


# ──────────────────────────────────────────────────────────────
# QUEUE PAGES (Tasks and Forms)
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
        form = TicketForm(initial={'client_name': request.user.get_full_name() or request.user.username})
    return render(request, 'tickets/create_ticket.html', {'form': form})


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
        ticket.approval_notes = request.POST.get('approval_notes', '').strip()

        if action == 'APPROVE':
            ticket.approval_status = 'APPROVED'
            messages.success(request, f"Ticket '{ticket.title}' approved and sent to IT.")
        elif action == 'REJECT':
            ticket.approval_status = 'REJECTED'
            ticket.status = 'CLOSED'
            messages.error(request, f"Ticket '{ticket.title}' rejected.")
        ticket.save()
        return redirect('head_dashboard')

    pending_approvals = Ticket.objects.filter(
        department=profile.department, approval_status='PENDING'
    ).order_by('-created_at')
    history_tickets = Ticket.objects.filter(
        department=profile.department
    ).exclude(approval_status='PENDING').order_by('-updated_at')

    if search_query:
        q = (
            Q(title__icontains=search_query) |
            Q(ticket_number__icontains=search_query) |
            Q(client_name__icontains=search_query) |
            Q(description__icontains=search_query)
        )
        pending_approvals = pending_approvals.filter(q)
        history_tickets   = history_tickets.filter(q)

    if status_filter:
        if view_mode == 'history':
            history_tickets   = history_tickets.filter(status=status_filter)
        else:
            pending_approvals = pending_approvals.filter(status=status_filter)

    ctx = {'view_mode': view_mode}
    if view_mode == 'history':
        ctx['history_tickets'] = history_tickets
    else:
        ctx['pending_approvals'] = pending_approvals
    return render(request, 'tickets/head_dashboard.html', ctx)


@login_required
def it_head_dashboard(request):
    check_and_generate_recurring_tasks()

    view_mode       = request.GET.get('view', 'active')
    search_query    = request.GET.get('q', '').strip()
    status_filter   = request.GET.get('status_filter', '')
    priority_filter = request.GET.get('priority_filter', '')

    if request.method == 'POST':
        if 'create_maintenance' in request.POST:
            title        = request.POST.get('title')
            description  = request.POST.get('description')
            recurrence   = request.POST.get('recurrence_type')
            tech_id      = request.POST.get('tech_id')
            priority     = request.POST.get('priority', 'MEDIUM')
            custom_start = request.POST.get('custom_date_start') or None
            custom_end   = request.POST.get('custom_date_end') or None

            if recurrence == 'CUSTOM' and custom_start and custom_end:
                if custom_start > custom_end:
                    messages.error(request, "Validation Error: Custom Start Date cannot be after End Date.")
                    return redirect('/tickets/it-head/?view=maintenance')

            assigned_tech = User.objects.filter(id=tech_id).first() if tech_id else None
            RecurringTask.objects.create(
                title=title, description=description, recurrence_type=recurrence,
                custom_date_start=custom_start, custom_date_end=custom_end,
                assigned_to=assigned_tech, priority=priority, created_by=request.user
            )
            messages.success(request, f"Maintenance schedule '{title}' created successfully!")
            return redirect('/tickets/it-head/?view=maintenance')

        elif 'ticket_id' in request.POST:
            ticket_id = request.POST.get('ticket_id')
            tech_id   = request.POST.get('tech_id')
            priority  = request.POST.get('priority', 'MEDIUM')
            ticket    = get_object_or_404(Ticket, id=ticket_id)
            tech      = get_object_or_404(User, id=tech_id)
            ticket.assigned_to    = tech
            ticket.priority       = priority
            ticket.status         = 'IN_PROGRESS'
            ticket.dispatch_notes = request.POST.get('dispatch_notes', '').strip()
            if not ticket.ticket_number:
                ticket.ticket_number = _generate_ticket_number()
            ticket.save()
            tech_name = tech.get_full_name() or tech.username
            messages.success(request, f"Ticket #{ticket.ticket_number} dispatched to {tech_name}!")
            return redirect('it_head_dashboard')

    unassigned_tickets = Ticket.objects.filter(
        status='OPEN', approval_status__in=['APPROVED', 'NOT_REQUIRED']
    ).order_by('-is_escalated', '-created_at')

    history_tickets    = Ticket.objects.filter(is_preventive_maintenance=False).exclude(status='OPEN').order_by('-updated_at')
    pm_history_tickets = Ticket.objects.filter(is_preventive_maintenance=True).order_by('-created_at')

    if priority_filter:
        unassigned_tickets = unassigned_tickets.filter(priority=priority_filter)
        history_tickets    = history_tickets.filter(priority=priority_filter)
        pm_history_tickets = pm_history_tickets.filter(priority=priority_filter)

    if search_query:
        q_base = (
            Q(title__icontains=search_query) |
            Q(ticket_number__icontains=search_query) |
            Q(client_name__icontains=search_query) |
            Q(description__icontains=search_query)
        )
        unassigned_tickets = unassigned_tickets.filter(q_base)
        history_tickets    = history_tickets.filter(
            q_base | Q(assigned_to__username__icontains=search_query)
        )
        pm_history_tickets = pm_history_tickets.filter(q_base)

    if status_filter:
        if view_mode == 'history':
            history_tickets = history_tickets.filter(status=status_filter)
        elif view_mode == 'pm_history':
            pm_history_tickets = pm_history_tickets.filter(status=status_filter)
        else:
            unassigned_tickets = unassigned_tickets.filter(status=status_filter)

    it_team = User.objects.filter(
        employeeprofile__department='IT',
        employeeprofile__is_department_head=False
    ).select_related('employeeprofile')

    active_history_list = pm_history_tickets if view_mode == 'pm_history' else history_tickets

    context = {
        'unassigned_tickets': unassigned_tickets,
        'history_tickets':    active_history_list,
        'maintenance_tasks':  RecurringTask.objects.all().order_by('-created_at'),
        'it_team':            it_team,
        'view_mode':          view_mode,
        'priority_filter':    priority_filter,
        'escalation_count':   _escalation_count(),
    }
    return render(request, 'tickets/it_head_dashboard.html', context)


@login_required
def it_staff_dashboard(request):
    try:
        profile = request.user.employeeprofile
        if profile.department != 'IT' or profile.is_department_head:
            return HttpResponseForbidden("Access Denied: IT Staff only.")
    except Exception:
        return HttpResponseForbidden("Access Denied.")

    view_mode       = request.GET.get('view', 'active')
    priority_filter = request.GET.get('priority_filter', '')
    status_filter   = request.GET.get('status_filter', '')
    type_filter     = request.GET.get('type_filter', '')

    if request.method == 'POST':
        ticket_id = request.POST.get('ticket_id')
        action    = request.POST.get('action')
        ticket    = get_object_or_404(Ticket, id=ticket_id, assigned_to=request.user)

        if action == 'ESCALATE':
            ticket.is_escalated       = True
            ticket.escalated_from_tier = profile.it_tier
            ticket.assigned_to        = None
            ticket.status             = 'OPEN'
            tier_label = profile.get_it_tier_display()
            messages.warning(request, f"Ticket '{ticket.title}' escalated from {tier_label} back to IT Head.")
        elif action == 'RESOLVE':
            ticket.status           = 'RESOLVED'
            ticket.resolution_notes = request.POST.get('resolution_notes', 'Resolved by IT.')
            messages.success(request, f"Ticket '{ticket.title}' marked as Resolved.")

        ticket.save()
        return redirect('it_staff_dashboard')

    my_stats = Ticket.objects.filter(
        assigned_to=request.user, status='CLOSED', client_feedback_rating__isnull=False
    ).aggregate(total_closed=Count('id'), average_rating=Avg('client_feedback_rating'))

    rating_tickets_qs = Ticket.objects.filter(
        assigned_to=request.user, status='CLOSED', client_feedback_rating__isnull=False
    ).order_by('-updated_at')

    rating_data = []
    for t in rating_tickets_qs:
        rating_data.append({
            'ticket_number': t.ticket_number or '—',
            'title':         t.title,
            'client_name':   t.client_name,
            'rating':        t.client_feedback_rating,
            'notes':         t.client_feedback_notes or '',
            'date':          t.updated_at.strftime('%b %d, %Y') if t.updated_at else '',
        })

    rating_breakdown = {i: rating_tickets_qs.filter(client_feedback_rating=i).count() for i in range(1, 6)}

    new_escalation_count = (
        Ticket.objects.filter(assigned_to=request.user, status='IN_PROGRESS', is_escalated=True).count()
        if profile.it_tier == 'TIER_2' else 0
    )

    base_ctx = {
        'my_stats':              my_stats,
        'view_mode':             view_mode,
        'profile':               profile,
        'rating_data_json':      json.dumps(rating_data),
        'rating_breakdown_json': json.dumps(rating_breakdown),
        'new_escalation_count':  new_escalation_count,
        'priority_filter':       priority_filter,
        'status_filter':         status_filter,
        'type_filter':           type_filter,
        'maintenance_tasks':     RecurringTask.objects.filter(is_active=True).order_by('-created_at'),
    }

    if view_mode == 'history':
        history_tickets = Ticket.objects.filter(
            assigned_to=request.user, status__in=['RESOLVED', 'CLOSED']
        ).order_by('-updated_at')
        if priority_filter:
            history_tickets = history_tickets.filter(priority=priority_filter)
        if status_filter:
            history_tickets = history_tickets.filter(status=status_filter)
        if type_filter == 'pm':
            history_tickets = history_tickets.filter(is_preventive_maintenance=True)
        elif type_filter == 'standard':
            history_tickets = history_tickets.filter(is_preventive_maintenance=False)
        return render(request, 'tickets/it_staff_dashboard.html', {**base_ctx, 'history_tickets': history_tickets})

    elif view_mode == 'maintenance':
        return render(request, 'tickets/it_staff_dashboard.html', base_ctx)

    else:
        my_tasks = Ticket.objects.filter(assigned_to=request.user, status='IN_PROGRESS').order_by('-created_at')
        if priority_filter:
            my_tasks = my_tasks.filter(priority=priority_filter)
        if type_filter == 'pm':
            my_tasks = my_tasks.filter(is_preventive_maintenance=True)
        elif type_filter == 'standard':
            my_tasks = my_tasks.filter(is_preventive_maintenance=False)
        return render(request, 'tickets/it_staff_dashboard.html', {**base_ctx, 'my_tasks': my_tasks})


def ticket_updates_sse(request):
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