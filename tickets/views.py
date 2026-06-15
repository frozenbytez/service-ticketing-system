from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden, StreamingHttpResponse, JsonResponse
from django.db.models import Count, Avg, Q, Case, When, IntegerField, Value
from django.contrib import messages
from django.views.decorators.http import require_POST
from .forms import TicketForm
from .models import Ticket, EmployeeProfile, DEPARTMENT_CHOICES, RecurringTask, Notification, TicketCategory
from django.utils import timezone
from datetime import timedelta, datetime, date
import calendar
import time
import json
from django.urls import reverse


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


# ──────────────────────────────────────────────────────────────
# NOTIFICATION HELPER
# ──────────────────────────────────────────────────────────────

def push_notification(recipient, notif_type, title, message, ticket=None):
    """
    Central helper to create a Notification record.
    Skips silently if recipient is None.
    """
    if not recipient:
        return
    Notification.objects.create(
        recipient=recipient,
        notif_type=notif_type,
        title=title,
        message=message,
        ticket=ticket,
    )


def _it_head_users():
    """Returns all IT department heads."""
    return User.objects.filter(
        employeeprofile__department='IT',
        employeeprofile__is_department_head=True,
    )


def _dept_head_of(department):
    """Returns the dept head user for a given department code (or None)."""
    return User.objects.filter(
        employeeprofile__department=department,
        employeeprofile__is_department_head=True,
    ).first()


# ──────────────────────────────────────────────────────────────
# NOTIFICATION API ENDPOINTS
# ──────────────────────────────────────────────────────────────

@login_required
def notifications_api(request):
    """
    GET  → return last 20 notifications as JSON
    POST → mark all as read
    """
    if request.method == 'POST':
        Notification.objects.filter(
            recipient=request.user, is_read=False
        ).update(is_read=True)
        return JsonResponse({'status': 'ok'})

    notifs = Notification.objects.filter(
        recipient=request.user
    ).select_related('ticket')[:20]

    def time_ago(dt):
        diff = timezone.now() - dt
        s = diff.total_seconds()
        if s < 60:
            return 'just now'
        elif s < 3600:
            return f'{int(s // 60)}m ago'
        elif s < 86400:
            return f'{int(s // 3600)}h ago'
        else:
            return f'{int(s // 86400)}d ago'

    data = []
    for n in notifs:
        data.append({
            'id':         n.id,
            'type':       n.notif_type,
            'title':      n.title,
            'message':    n.message,
            'is_read':    n.is_read,
            'time_ago':   time_ago(n.created_at),
            'ticket_num': n.ticket.ticket_number if n.ticket and n.ticket.ticket_number else None,
        })

    unread = Notification.objects.filter(recipient=request.user, is_read=False).count()
    return JsonResponse({'notifications': data, 'unread_count': unread})


# ──────────────────────────────────────────────────────────────
# RECURRING TASK GENERATOR (triggers PM notifications)
# ──────────────────────────────────────────────────────────────

def check_and_generate_recurring_tasks():
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
            # Guard: skip if there is already an active (unresolved) ticket for this task
            already_active = Ticket.objects.filter(
                source_recurring_task=task,
                status__in=['OPEN', 'IN_PROGRESS']
            ).exists()
            if already_active:
                continue

            # ── NEW: CALCULATE EXACT DEADLINE ──
            ticket_due_date = today
            if task.recurrence_type == 'DAILY':
                ticket_due_date = today
            elif task.recurrence_type == 'WEEKLY':
                # Deadline is Sunday of the current week
                ticket_due_date = today + timedelta(days=6 - today.weekday())
            elif task.recurrence_type in ['MONTHLY', 'QUARTERLY']:
                # Deadline is the very last day of the current month
                last_day = calendar.monthrange(today.year, today.month)[1]
                ticket_due_date = today.replace(day=last_day)
            elif task.recurrence_type == 'CUSTOM' and task.custom_date_end:
                ticket_due_date = task.custom_date_end

            notes = f"Auto-generated for {task.get_recurrence_type_display()} maintenance schedule."
            if task.recurrence_type == 'CUSTOM' and task.custom_date_start and task.custom_date_end:
                notes += f" (Must be completed between {task.custom_date_start.strftime('%b %d')} and {task.custom_date_end.strftime('%b %d, %Y')})"

            # Locate the Ticket.objects.create(...) block and add the time fields:
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
                source_recurring_task=task,
                due_date=ticket_due_date,
                
                # ---> ADD THESE TWO LINES <---
                scheduled_start_time=task.start_time or '08:00',
                scheduled_end_time=task.end_time or '17:00',
                
                dispatch_notes=notes
            )
            ticket.ticket_number = _generate_ticket_number()
            ticket.save()

            task.last_generated = today
            task.save()

            # ── NOTIFICATION: IT Staff — PM task posted ──────────
            if task.assigned_to:
                push_notification(
                    recipient=task.assigned_to,
                    notif_type='pm_announced',
                    title='New PM Task Posted',
                    message=(
                        f'A new preventive maintenance task has been scheduled for you: '
                        f'"{task.title}" ({task.get_recurrence_type_display()}).'
                    ),
                    ticket=ticket,
                )


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

    total_sent      = tickets.count()
    total_approved  = tickets.filter(approval_status='APPROVED').count()
    total_declined  = tickets.filter(approval_status='REJECTED').count()
    total_pending   = tickets.filter(approval_status='PENDING').count()
    total_fixed     = tickets.filter(status__in=['RESOLVED', 'CLOSED']).count()
    total_feedbacks = tickets.filter(client_feedback_rating__isnull=False).count()

    priority_data = [
        tickets.filter(priority='LOW').count(),
        tickets.filter(priority='MEDIUM').count(),
        tickets.filter(priority='HIGH').count(),
        tickets.filter(priority='URGENT').count(),
    ]
    status_data = [
        tickets.filter(status='OPEN').count(),
        tickets.filter(status='IN_PROGRESS').count(),
        tickets.filter(status='RESOLVED').count(),
        tickets.filter(status='CLOSED').count(),
    ]

    all_my_tickets = Ticket.objects.filter(requester=request.user)
    monthly_labels, monthly_counts = _monthly_trend(all_my_tickets)

    context = {
        'time_filter':           time_filter,
        'total_sent':            total_sent,
        'total_approved':        total_approved,
        'total_declined':        total_declined,
        'total_fixed':           total_fixed,
        'total_feedbacks':       total_feedbacks,
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

    total_sent      = tickets.count()
    total_approved  = tickets.filter(approval_status='APPROVED').count()
    total_declined  = tickets.filter(approval_status='REJECTED').count()
    total_pending   = tickets.filter(approval_status='PENDING').count()
    total_fixed     = tickets.filter(status__in=['RESOLVED', 'CLOSED']).count()
    total_feedbacks = tickets.filter(client_feedback_rating__isnull=False).count()

    priority_data = [
        tickets.filter(priority='LOW').count(),
        tickets.filter(priority='MEDIUM').count(),
        tickets.filter(priority='HIGH').count(),
        tickets.filter(priority='URGENT').count(),
    ]
    status_data = [
        tickets.filter(status='OPEN').count(),
        tickets.filter(status='IN_PROGRESS').count(),
        tickets.filter(status='RESOLVED').count(),
        tickets.filter(status='CLOSED').count(),
    ]

    all_dept_tickets = Ticket.objects.filter(department=dept)
    monthly_labels, monthly_counts = _monthly_trend(all_dept_tickets)

    context = {
        'time_filter':           time_filter,
        'total_sent':            total_sent,
        'total_approved':        total_approved,
        'total_declined':        total_declined,
        'total_fixed':           total_fixed,
        'total_feedbacks':       total_feedbacks,
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

    total_sent      = tickets.count()
    total_approved  = tickets.filter(approval_status='APPROVED').count()
    total_declined  = tickets.filter(approval_status='REJECTED').count()
    total_pending   = tickets.filter(approval_status='PENDING').count()
    total_assigned  = tickets.filter(assigned_to__isnull=False).count()
    total_fixed     = tickets.filter(status__in=['RESOLVED', 'CLOSED']).count()
    total_escalated = tickets.filter(is_escalated=True).count()

    dept_counts       = tickets.values('department').annotate(count=Count('id'))
    chart_depts       = [d['department'] for d in dept_counts]
    chart_dept_counts = [d['count']      for d in dept_counts]

    status_data = [
        tickets.filter(status='OPEN').count(),
        tickets.filter(status='IN_PROGRESS').count(),
        tickets.filter(status='RESOLVED').count(),
        tickets.filter(status='CLOSED').count(),
    ]
    priority_data = [
        tickets.filter(priority='LOW').count(),
        tickets.filter(priority='MEDIUM').count(),
        tickets.filter(priority='HIGH').count(),
        tickets.filter(priority='URGENT').count(),
    ]

    monthly_labels, monthly_counts = _monthly_trend(Ticket.objects.all())

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
        agg  = staff_tix.aggregate(avg_rating=Avg('client_feedback_rating'), total_feedbacks=Count('id'))
        tier = staff.employeeprofile.get_it_tier_display() if hasattr(staff, 'employeeprofile') else '—'
        staff_stats.append({
            'name':            staff.get_full_name() or staff.username,
            'tier':            tier,
            'assigned':        tickets.filter(assigned_to=staff).count(),
            'resolved':        tickets.filter(assigned_to=staff, status__in=['RESOLVED', 'CLOSED']).count(),
            'avg_rating':      round(agg['avg_rating'], 1) if agg['avg_rating'] else 0.0,
            'total_feedbacks': agg['total_feedbacks'] or 0,
        })
    staff_stats_sorted = sorted(staff_stats, key=lambda x: x['avg_rating'], reverse=True)

    staff_names    = [s['name']       for s in staff_stats_sorted]
    staff_resolved = [s['resolved']   for s in staff_stats_sorted]
    staff_ratings  = [s['avg_rating'] for s in staff_stats_sorted]

    context = {
        'time_filter':            time_filter,
        'dept_filter':            dept_filter,
        'departments':            [d[0] for d in DEPARTMENT_CHOICES],
        'total_sent':             total_sent,
        'total_approved':         total_approved,
        'total_declined':         total_declined,
        'total_assigned':         total_assigned,
        'total_fixed':            total_fixed,
        'total_escalated':        total_escalated,
        'staff_stats':            staff_stats_sorted,
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

    total_assigned = tickets.count()
    total_resolved = tickets.filter(status__in=['RESOLVED', 'CLOSED']).count()
    total_active   = tickets.filter(status='IN_PROGRESS').count()
    total_open     = tickets.filter(status='OPEN').count()

    most_dept_qs = (
        tickets.values('department')
               .annotate(count=Count('id'))
               .order_by('-count')
               .first()
    )
    dept_display = dict(DEPARTMENT_CHOICES)
    most_dept = dept_display.get(most_dept_qs['department'], '—') if most_dept_qs else '—'

    dept_counts       = tickets.values('department').annotate(count=Count('id'))
    chart_depts       = [d['department'] for d in dept_counts]
    chart_dept_counts = [d['count']      for d in dept_counts]

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

    priority_data = [
        tickets.filter(priority='LOW').count(),
        tickets.filter(priority='MEDIUM').count(),
        tickets.filter(priority='HIGH').count(),
        tickets.filter(priority='URGENT').count(),
    ]
    status_data = [total_active, total_resolved, total_open]

    # Count escalated tickets for this staff member (all-time, not time-filtered)
    total_escalated = Ticket.objects.filter(
        Q(escalated_by=request.user) |                    # escalated BY me (Tier 1)
        Q(assigned_to=request.user, is_escalated=True)    # escalated TO me (Tier 2)
    ).distinct().count()

    all_my_tickets = Ticket.objects.filter(assigned_to=request.user)
    monthly_labels, monthly_counts = _monthly_trend(all_my_tickets)

    context = {
        'time_filter':            time_filter,
        'total_assigned':         total_assigned,
        'total_resolved':         total_resolved,
        'total_active':           total_active,
        'total_escalated':        total_escalated,
        'most_dept':              most_dept,
        'total_feedbacks':        agg['count'] or 0,
        'avg_rating':             round(agg['avg'], 1) if agg['avg'] else 0.0,
        'rating_data':            rating_data,
        'max_rating':             max_rating_count,
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
# QUEUE PAGES
# ──────────────────────────────────────────────────────────────

@login_required
def clinic_portal(request):
 
    # ── Handle feedback submission ────────────────────────────────
    if request.method == 'POST' and 'submit_feedback' in request.POST:
        ticket_id = request.POST.get('ticket_id')
        rating    = request.POST.get('rating')
        notes     = request.POST.get('feedback_notes', '').strip()
        ticket    = get_object_or_404(Ticket, id=ticket_id, requester=request.user, status='RESOLVED')
 
        try:
            rating = int(rating)
            if not 1 <= rating <= 5:
                raise ValueError
        except (TypeError, ValueError):
            messages.error(request, "Please select a valid rating between 1 and 5.")
            return redirect('clinic_portal')
 
        ticket.client_feedback_rating = rating
        ticket.client_feedback_notes  = notes
        ticket.status                 = 'CLOSED'
        ticket.save()
 
        # ── NOTIFICATION: IT Staff — received a rating ────────
        if ticket.assigned_to:
            push_notification(
                recipient=ticket.assigned_to,
                notif_type='feedback_received',
                title='You Received a Rating',
                message=(
                    f'You received a {rating}★ rating on '
                    f'"{ticket.title}" from {request.user.get_full_name() or request.user.username}.'
                ),
                ticket=ticket,
            )
 
        # ── NOTIFICATION: IT Head — rating info ──────────────
        for it_head in _it_head_users():
            push_notification(
                recipient=it_head,
                notif_type='rating_received',
                title='IT Staff Received a Rating',
                message=(
                    f'{ticket.assigned_to.get_full_name() or ticket.assigned_to.username} '
                    f'received {rating}★ on ticket "{ticket.title}".'
                ),
                ticket=ticket,
            )
 
        messages.success(request, "Thank you for your feedback! The ticket is now closed.")
        return redirect('clinic_portal')
 
    # ── Handle reopen ─────────────────────────────────────────────
    if request.method == 'POST' and 'reopen_ticket' in request.POST:
        ticket_id     = request.POST.get('ticket_id')
        reopen_reason = request.POST.get('reopen_reason', '').strip()
        ticket        = get_object_or_404(Ticket, id=ticket_id, requester=request.user, status='CLOSED')
 
        if not reopen_reason:
            messages.error(request, "Please provide a reason for reopening before submitting.")
            return redirect('clinic_portal')
 
        ticket.status               = 'OPEN'
        ticket.assigned_to          = None
        ticket.approval_status      = 'APPROVED'
        ticket.needs_head_approval  = False
        ticket.is_escalated         = False
        ticket.escalated_from_tier  = None
        ticket.resolution_notes     = None
        ticket.client_feedback_rating = None
        ticket.client_feedback_notes  = None
        ticket.reopen_reason        = reopen_reason
        ticket.reopened_at          = timezone.now()
        ticket.reopen_count         = (ticket.reopen_count or 0) + 1
        ticket.save()
 
        for it_head in _it_head_users():
            push_notification(
                recipient=it_head,
                notif_type='ticket_reopened',
                title='Ticket Reopened — Needs Reassignment',
                message=(
                    f'Ticket #{ticket.ticket_number or ticket.id} "{ticket.title}" was reopened by '
                    f'{request.user.get_full_name() or request.user.username}. '
                    f'Reason: {reopen_reason}'
                ),
                ticket=ticket,
            )
 
        messages.success(
            request,
            f"Ticket '{ticket.title}' has been reopened and sent back to IT for reassignment."
        )
        return redirect('clinic_portal')
 
    # ── Filters ───────────────────────────────────────────────────
    search_query    = request.GET.get('q', '').strip()
    status_filter   = request.GET.get('status_filter', '')
    approval_filter = request.GET.get('approval_filter', '')   # ← NEW
 
    # ── Query: RESOLVED tickets sort to top so employee sees
    #    "needs feedback" items first, everything else by newest.
    my_tickets = (
        Ticket.objects.filter(requester=request.user)
        .annotate(
            sort_order=Case(
                When(status='RESOLVED', then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by('sort_order', '-created_at')
    )
 
    if search_query:
        my_tickets = my_tickets.filter(
            Q(title__icontains=search_query) |
            Q(ticket_number__icontains=search_query) |
            Q(description__icontains=search_query)
        )
    if status_filter:
        my_tickets = my_tickets.filter(status=status_filter)
    if approval_filter:                                        # ← NEW
        my_tickets = my_tickets.filter(approval_status=approval_filter)
 
    # Count of tickets pending feedback (for the banner) ← NEW
    needs_feedback_count = Ticket.objects.filter(
        requester=request.user, status='RESOLVED'
    ).count()
 
    return render(request, 'tickets/clinic_portal.html', {
        'my_tickets':           my_tickets,
        'needs_feedback_count': needs_feedback_count,         # ← NEW
    })


@login_required
def create_ticket(request):
    if request.method == 'POST':
        form = TicketForm(request.POST)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.category          = request.POST.get('category', 'OTHER')   # ← ADD
            ticket.requester         = request.user
            ticket.department        = request.user.employeeprofile.department
            ticket.priority          = form.cleaned_data.get('priority', 'MEDIUM')
            ticket.needs_head_approval = True
            ticket.approval_status   = 'PENDING'
            ticket.ticket_number     = _generate_ticket_number()
            ticket.save()

            # ── NOTIFICATION: Dept Head — new ticket needs approval ──
            dept_head = _dept_head_of(ticket.department)
            if dept_head:
                push_notification(
                    recipient=dept_head,
                    notif_type='needs_approval',
                    title='Ticket Needs Your Approval',
                    message=(
                        f'{request.user.get_full_name() or request.user.username} submitted '
                        f'"{ticket.title}" and it requires your approval.'
                    ),
                    ticket=ticket,
                )

            messages.success(request, "Request submitted successfully!")
            return redirect('clinic_portal')
    else:
        form = TicketForm(initial={'client_name': request.user.get_full_name() or request.user.username})
    categories = TicketCategory.objects.filter(is_active=True).order_by('sort_order', 'label')
    return render(request, 'tickets/create_ticket.html', {'form': form, 'categories': categories})


@login_required
def head_dashboard(request):
    try:
        profile = request.user.employeeprofile
        if not profile.is_department_head or profile.department == 'IT':
            return HttpResponseForbidden("Access Denied: Non-IT Department Heads only.")
    except Exception:
        return HttpResponseForbidden("Access Denied.")

    view_mode     = request.GET.get('view', 'active')
    search_query  = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status_filter', '')

    if request.method == 'POST' and 'handle_approval' in request.POST:
        ticket_id = request.POST.get('ticket_id')
        action    = request.POST.get('action')
        ticket    = get_object_or_404(Ticket, id=ticket_id, department=profile.department)
        ticket.approval_notes = request.POST.get('approval_notes', '').strip()

        if action == 'APPROVE':
            ticket.approval_status = 'APPROVED'
            ticket.save()
            messages.success(request, f"Ticket '{ticket.title}' approved and sent to IT.")

            # ── NOTIFICATION: Employee — ticket approved ──────────
            push_notification(
                recipient=ticket.requester,
                notif_type='ticket_approved',
                title='Your Ticket Was Approved',
                message=(
                    f'Your ticket "{ticket.title}" has been approved by '
                    f'{request.user.get_full_name() or request.user.username} '
                    f'and is now in the IT queue.'
                ),
                ticket=ticket,
            )

            # ── NOTIFICATION: IT Head — new ticket needs assigning ─
            for it_head in _it_head_users():
                push_notification(
                    recipient=it_head,
                    notif_type='needs_assigning',
                    title='New Ticket Needs Assigning',
                    message=(
                        f'"{ticket.title}" from {ticket.get_department_display()} '
                        f'has been approved and is waiting for an IT staff assignment.'
                    ),
                    ticket=ticket,
                )

        elif action == 'REJECT':
            ticket.approval_status = 'REJECTED'
            ticket.status          = 'CLOSED'
            ticket.save()
            messages.error(request, f"Ticket '{ticket.title}' rejected.")

            # ── NOTIFICATION: Employee — ticket rejected ──────────
            push_notification(
                recipient=ticket.requester,
                notif_type='ticket_rejected',
                title='Your Ticket Was Declined',
                message=(
                    f'Your ticket "{ticket.title}" was declined by '
                    f'{request.user.get_full_name() or request.user.username}.'
                    + (f' Note: {ticket.approval_notes}' if ticket.approval_notes else '')
                ),
                ticket=ticket,
            )

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
        ctx['history_tickets']   = history_tickets
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
        action_type = request.POST.get('action_type')

        if action_type == 'create_maintenance':
            title        = request.POST.get('title')
            description  = request.POST.get('description')
            recurrence   = request.POST.get('recurrence_type')
            tech_id      = request.POST.get('tech_id')
            priority     = request.POST.get('priority', 'MEDIUM')
            custom_start = request.POST.get('custom_date_start') or None
            custom_end   = request.POST.get('custom_date_end') or None
            
            # ---> 1. DEFINE THE VARIABLES HERE <---
            start_time   = request.POST.get('start_time') or '08:00'
            end_time     = request.POST.get('end_time') or '17:00'

            if recurrence == 'CUSTOM' and custom_start and custom_end:
                if custom_start > custom_end:
                    messages.error(request, "Validation Error: Custom Start Date cannot be after End Date.")
                    return redirect('/tickets/it-head/?view=maintenance')

            assigned_tech = User.objects.filter(id=tech_id).first() if tech_id else None
            
            # ---> 2. PASS THE VARIABLES TO THE DATABASE <---
            RecurringTask.objects.create(
                title=title, description=description, recurrence_type=recurrence,
                custom_date_start=custom_start, custom_date_end=custom_end,
                start_time=start_time, end_time=end_time, 
                assigned_to=assigned_tech, priority=priority, created_by=request.user
            )
            messages.success(request, f"Maintenance schedule '{title}' created successfully!")
            return redirect('/tickets/it-head/?view=maintenance')

        elif action_type == 'edit_maintenance':
            task_id = request.POST.get('task_id')
            task = get_object_or_404(RecurringTask, id=task_id)
            
            task.title = request.POST.get('title')
            task.description = request.POST.get('description')
            task.recurrence_type = request.POST.get('recurrence_type')
            
            tech_id = request.POST.get('tech_id')
            task.assigned_to = User.objects.filter(id=tech_id).first() if tech_id else None
            
            task.priority = request.POST.get('priority', 'MEDIUM')
            
            custom_start = request.POST.get('custom_date_start') or None
            custom_end = request.POST.get('custom_date_end') or None
            
            
            if task.recurrence_type == 'CUSTOM' and custom_start and custom_end:
                if custom_start > custom_end:
                    messages.error(request, "Validation Error: Custom Start Date cannot be after End Date.")
                    return redirect('/tickets/it-head/?view=maintenance')
                    
            task.custom_date_start = custom_start
            task.custom_date_end = custom_end
            task.start_time = request.POST.get('start_time') or '08:00'
            task.end_time = request.POST.get('end_time') or '17:00'
            task.save()
            
            messages.success(request, f"Maintenance schedule '{task.title}' updated successfully!")
            return redirect('/tickets/it-head/?view=maintenance')

        # ... keep delete_maintenance and review_pm exactly as they are below ...

            # ── NEW: Remove a PM Schedule ──
        elif 'delete_maintenance' in request.POST:
            task_id = request.POST.get('task_id')
            task = get_object_or_404(RecurringTask, id=task_id)
            task_title = task.title
            task.delete()
            messages.success(request, f"Maintenance schedule '{task_title}' has been successfully removed.")
            return redirect('/tickets/it-head/?view=maintenance')

            if recurrence == 'CUSTOM' and custom_start and custom_end:
                if custom_start > custom_end:
                    messages.error(request, "Validation Error: Custom Start Date cannot be after End Date.")
                    return redirect('/tickets/it-head/?view=maintenance')

            assigned_tech = User.objects.filter(id=tech_id).first() if tech_id else None
            RecurringTask.objects.create(
                title=title, description=description, recurrence_type=recurrence,
                custom_date_start=custom_start, custom_date_end=custom_end,
                assigned_to=assigned_tech, priority=priority, created_by=request.user,
            )
            messages.success(request, f"Maintenance schedule '{title}' created successfully!")
            return redirect('/tickets/it-head/?view=maintenance')

        # ── PM Review ───────────────────────────────────────────
        elif 'review_pm' in request.POST:
            ticket_id  = request.POST.get('ticket_id')
            ticket     = get_object_or_404(Ticket, id=ticket_id, is_preventive_maintenance=True, status='RESOLVED')
            raw_rating = request.POST.get('pm_review_rating', '').strip()
            ticket.pm_review_rating = int(raw_rating) if raw_rating.isdigit() else None
            ticket.pm_review_notes  = request.POST.get('pm_review_notes', '').strip()
            ticket.pm_reviewed_by   = request.user
            ticket.pm_reviewed_at   = timezone.now()
            ticket.status           = 'CLOSED'
            ticket.save()
            messages.success(request, f"PM Ticket '{ticket.title}' reviewed and closed.")
            return redirect('/tickets/it-head/?view=pm_history')

        # ── Category CRUD ───────────────────────────────────────
        elif action_type == 'create_category':
            key   = request.POST.get('cat_key', '').strip().upper().replace(' ', '_')
            label = request.POST.get('cat_label', '').strip()
            icon  = request.POST.get('cat_icon', 'fa-solid fa-tag').strip()
            sort_order = int(request.POST.get('cat_sort_order', 0) or 0)
            if key and label:
                if TicketCategory.objects.filter(key=key).exists():
                    messages.error(request, f"A category with key '{key}' already exists.")
                else:
                    TicketCategory.objects.create(key=key, label=label, icon=icon, sort_order=sort_order)
                    messages.success(request, f"Category '{label}' created successfully!")
            else:
                messages.error(request, "Category key and label are required.")
            return redirect('/tickets/it-head/?view=categories')

        elif action_type == 'edit_category':
            cat_id = request.POST.get('cat_id')
            cat    = get_object_or_404(TicketCategory, id=cat_id)
            new_key = request.POST.get('cat_key', '').strip().upper().replace(' ', '_')
            if new_key and new_key != cat.key and TicketCategory.objects.filter(key=new_key).exists():
                messages.error(request, f"A category with key '{new_key}' already exists.")
                return redirect('/tickets/it-head/?view=categories')
            cat.key        = new_key or cat.key
            cat.label      = request.POST.get('cat_label', cat.label).strip()
            cat.icon       = request.POST.get('cat_icon', cat.icon).strip() or 'fa-solid fa-tag'
            cat.sort_order = int(request.POST.get('cat_sort_order', cat.sort_order) or 0)
            cat.save()
            messages.success(request, f"Category '{cat.label}' updated successfully!")
            return redirect('/tickets/it-head/?view=categories')

        elif action_type == 'toggle_category':
            cat = get_object_or_404(TicketCategory, id=request.POST.get('cat_id'))
            cat.is_active = not cat.is_active
            cat.save()
            state = "activated" if cat.is_active else "deactivated"
            messages.success(request, f"Category '{cat.label}' {state}.")
            return redirect('/tickets/it-head/?view=categories')

        elif action_type == 'delete_category':
            cat = get_object_or_404(TicketCategory, id=request.POST.get('cat_id'))
            label = cat.label
            cat.delete()
            messages.success(request, f"Category '{label}' deleted.")
            return redirect('/tickets/it-head/?view=categories')

        # ── Dispatch ticket to IT staff ─────────────────────────
        elif 'ticket_id' in request.POST:
            ticket_id = request.POST.get('ticket_id')
            tech_id   = request.POST.get('tech_id')
            priority  = request.POST.get('priority', 'MEDIUM')
            ticket    = get_object_or_404(Ticket, id=ticket_id)
            tech      = get_object_or_404(User, id=tech_id)

            # ── NEW: Prevent assigning Tier 1 escalated tickets back to Tier 1 ──
            if ticket.is_escalated and ticket.escalated_from_tier == 'TIER_1' and tech.employeeprofile.it_tier == 'TIER_1':
                messages.error(request, f"Error: Ticket '{ticket.title}' was escalated by Tier 1. It must be assigned to a Tier 2 technician.")
                return redirect('it_head_dashboard')

            ticket.assigned_to    = tech
            ticket.priority       = priority
            ticket.status         = 'IN_PROGRESS'
            ticket.dispatch_notes = request.POST.get('dispatch_notes', '').strip()
            if not ticket.ticket_number:
                ticket.ticket_number = _generate_ticket_number()
            ticket.save()
            tech_name = tech.get_full_name() or tech.username
            messages.success(request, f"Ticket #{ticket.ticket_number} dispatched to {tech_name}!")

            # ── NOTIFICATION: IT Staff — ticket assigned ──────────
            push_notification(
                recipient=tech,
                notif_type='ticket_assigned',
                title='New Ticket Assigned to You',
                message=(
                    f'Ticket #{ticket.ticket_number} "{ticket.title}" '
                    f'({ticket.get_priority_display()} priority) has been assigned to you.'
                    + (f'\nNote: {ticket.dispatch_notes}' if ticket.dispatch_notes else '')
                ),
                ticket=ticket,
            )

            # ── NOTIFICATION: Dept Head — ticket dispatched ───────
            dept_head = _dept_head_of(ticket.department)
            if dept_head:
                push_notification(
                    recipient=dept_head,
                    notif_type='ticket_dispatched',
                    title='Ticket Assigned to IT Staff',
                    message=(
                        f'Ticket "{ticket.title}" from your department has been '
                        f'assigned to {tech_name} and is now In Progress.'
                    ),
                    ticket=ticket,
                )

            return redirect('it_head_dashboard')

    # ── Querysets ───────────────────────────────────────────────
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
        history_tickets    = history_tickets.filter(q_base | Q(assigned_to__username__icontains=search_query))
        pm_history_tickets = pm_history_tickets.filter(q_base)

    if status_filter:
        if view_mode == 'history':
            history_tickets    = history_tickets.filter(status=status_filter)
        elif view_mode == 'pm_history':
            pm_history_tickets = pm_history_tickets.filter(status=status_filter)
        else:
            unassigned_tickets = unassigned_tickets.filter(status=status_filter)

    it_team = User.objects.filter(
        employeeprofile__department='IT',
        employeeprofile__is_department_head=False,
    ).select_related('employeeprofile')

    active_history_list      = pm_history_tickets if view_mode == 'pm_history' else history_tickets
    pending_pm_reviews_count = Ticket.objects.filter(
        is_preventive_maintenance=True, status='RESOLVED'
    ).count()

    context = {
        'unassigned_tickets':       unassigned_tickets,
        'history_tickets':          active_history_list,
        'maintenance_tasks':        RecurringTask.objects.all().order_by('-created_at'),
        'it_team':                  it_team,
        'view_mode':                view_mode,
        'priority_filter':          priority_filter,
        'escalation_count':         _escalation_count(),
        'pending_pm_reviews_count': pending_pm_reviews_count,
        'all_categories':           TicketCategory.objects.all().order_by('sort_order', 'label'),
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
                ticket.is_escalated        = True
                ticket.escalated_from_tier = profile.it_tier
                ticket.escalated_by        = request.user         
                ticket.assigned_to         = None
                ticket.status              = 'OPEN'
                tier_label = profile.get_it_tier_display()
                ticket.save()
                messages.warning(request, f"Ticket '{ticket.title}' escalated from {tier_label} back to IT Head.")

                # ── NOTIFICATION: IT Head — ticket escalated/back in queue ─
                for it_head in _it_head_users():
                    push_notification(
                        recipient=it_head,
                        notif_type='needs_assigning',
                        title='Escalated Ticket Needs Reassigning',
                        message=(
                            f'Ticket "{ticket.title}" was escalated by '
                            f'{request.user.get_full_name() or request.user.username} '
                            f'({tier_label}) and needs to be reassigned.'
                        ),
                        ticket=ticket,
                    )

                # ── NOTIFICATION: Tier 2 IT staff — escalated ticket ──
                if profile.it_tier == 'TIER_1':
                    tier2_staff = User.objects.filter(
                        employeeprofile__department='IT',
                        employeeprofile__is_department_head=False,
                        employeeprofile__it_tier='TIER_2',
                    )
                    for staff in tier2_staff:
                        push_notification(
                            recipient=staff,
                            notif_type='ticket_escalated',
                            title='Ticket Escalated',
                            message=(
                                f'Ticket "{ticket.title}" was escalated from Tier 1 '
                                f'by {request.user.get_full_name() or request.user.username} '
                                f'and may be assigned to you.'
                            ),
                            ticket=ticket,
                        )

            # ---> NEW: Clean START action <---
            elif action == 'START':
                ticket.started_at = timezone.now()
                ticket.save()
                messages.success(request, f"Started working on '{ticket.title}'. Time tracking has begun.")

            # ---> REPAIRED: RESOLVE action with safely indented notifications <---
            elif action == 'RESOLVE':
                ticket.status           = 'RESOLVED'
                ticket.resolved_at      = timezone.now()          
                ticket.resolution_notes = request.POST.get('resolution_notes', 'Resolved by IT.')
                
                # Fallback: if they somehow bypassed the start button, set start to now
                if not ticket.started_at:
                    ticket.started_at = ticket.resolved_at
                    
                ticket.save()
                messages.success(request, f"Ticket '{ticket.title}' marked as resolved.")

                # ── NOTIFICATION: Employee — ticket resolved ───────────
                push_notification(
                    recipient=ticket.requester,
                    notif_type='ticket_resolved',
                    title='Your Ticket Has Been Resolved',
                    message=(
                        f'Your ticket "{ticket.title}" has been resolved by '
                        f'{request.user.get_full_name() or request.user.username}. '
                        f'Please review and close it.'
                    ),
                    ticket=ticket,
                )

                # ── NOTIFICATION: Dept Head — ticket resolved ─────────
                dept_head = _dept_head_of(ticket.department)
                if dept_head and dept_head != ticket.requester:
                    push_notification(
                        recipient=dept_head,
                        notif_type='ticket_resolved',
                        title='Department Ticket Resolved',
                        message=(
                            f'Ticket "{ticket.title}" submitted by '
                            f'{ticket.requester.get_full_name() or ticket.requester.username} '
                            f'has been resolved.'
                        ),
                        ticket=ticket,
                    )

                # ── NOTIFICATION: IT Head — IT staff resolved ticket ──
                for it_head in _it_head_users():
                    push_notification(
                        recipient=it_head,
                        notif_type='it_resolved',
                        title='IT Staff Resolved a Ticket',
                        message=(
                            f'{request.user.get_full_name() or request.user.username} resolved '
                            f'"{ticket.title}" — awaiting client feedback.'
                        ),
                        ticket=ticket,
                    )

                # ── NOTIFICATION: IT Head — PM task finished ──────────
                if ticket.is_preventive_maintenance:
                    for it_head in _it_head_users():
                        push_notification(
                            recipient=it_head,
                            notif_type='pm_finished',
                            title='PM Task Completed',
                            message=(
                                f'{request.user.get_full_name() or request.user.username} '
                                f'completed preventive maintenance task "{ticket.title}". '
                                f'Please review it in the PM History tab.'
                            ),
                            ticket=ticket,
                        )

            return redirect('it_staff_dashboard')

    # ── Build context ────────────────────────────────────────────
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
        else:
            my_tasks = my_tasks.filter(is_preventive_maintenance=False)
        return render(request, 'tickets/it_staff_dashboard.html', {**base_ctx, 'my_tasks': my_tasks})


# ──────────────────────────────────────────────────────────────
# SSE — REAL-TIME PAGE REFRESH
# ──────────────────────────────────────────────────────────────

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
    response['Cache-Control']     = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response

# ──────────────────────────────────────────────────────────────
# NOTIFICATIONS INBOX (full-page)
# ──────────────────────────────────────────────────────────────

# Icon / colour map — used to pre-annotate each notification
_NOTIF_META = {
    'ticket_approved':   ('fa-circle-check',          '#059669', '#ecfdf5'),
    'ticket_rejected':   ('fa-circle-xmark',          '#dc2626', '#fef2f2'),
    'ticket_resolved':   ('fa-screwdriver-wrench',    '#2563eb', '#eff6ff'),
    'needs_approval':    ('fa-triangle-exclamation',  '#d97706', '#fffbeb'),
    'ticket_dispatched': ('fa-paper-plane',           '#2563eb', '#eff6ff'),
    'needs_assigning':   ('fa-inbox',                 '#d97706', '#fffbeb'),
    'ticket_reopened':   ('fa-rotate-left',           '#b45309', '#fff7ed'),
    'pm_finished':       ('fa-calendar-check',        '#00c4b8', '#e0faf8'),
    'it_resolved':       ('fa-circle-check',          '#059669', '#ecfdf5'),
    'rating_received':   ('fa-star',                  '#d97706', '#fffbeb'),
    'ticket_assigned':   ('fa-user-gear',             '#4f46e5', '#e0e7ff'),
    'pm_announced':      ('fa-calendar-plus',         '#00c4b8', '#e0faf8'),
    'feedback_received': ('fa-comment-dots',          '#d97706', '#fffbeb'),
    'ticket_escalated':  ('fa-arrow-up-right-dots',   '#dc2626', '#fef2f2'),
}


@login_required
def notifications_inbox(request):
    from django.core.paginator import Paginator

    # ── Mark-all-read via POST ──────────────────────────────
    if request.method == 'POST' and 'mark_all_read' in request.POST:
        Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
        params = request.GET.copy()
        qs = params.urlencode()
        return redirect(f"{reverse('notifications_inbox')}{'?' + qs if qs else ''}")

    filter_type = request.GET.get('filter', 'all')   # all | unread | read
    type_filter = request.GET.get('type', '')

    base_qs = Notification.objects.filter(
        recipient=request.user
    ).select_related('ticket').order_by('-created_at')

    all_count    = base_qs.count()
    unread_count = base_qs.filter(is_read=False).count()
    read_count   = base_qs.filter(is_read=True).count()

    filtered_qs = base_qs
    if filter_type == 'unread':
        filtered_qs = filtered_qs.filter(is_read=False)
    elif filter_type == 'read':
        filtered_qs = filtered_qs.filter(is_read=True)

    if type_filter:
        filtered_qs = filtered_qs.filter(notif_type=type_filter)

    paginator   = Paginator(filtered_qs, 25)
    page_number = request.GET.get('page', 1)
    page_obj    = paginator.get_page(page_number)

    # ── Annotate each notification with display helpers ─────
    today     = timezone.localdate()
    yesterday = today - timedelta(days=1)

    for notif in page_obj:
        # Date group label
        notif_date = timezone.localtime(notif.created_at).date()
        days_ago   = (today - notif_date).days
        if notif_date == today:
            notif.date_group = 'Today'
        elif notif_date == yesterday:
            notif.date_group = 'Yesterday'
        elif days_ago < 7:
            notif.date_group = 'This Week'
        else:
            notif.date_group = 'Older'

        # Icon / colour
        meta = _NOTIF_META.get(notif.notif_type, ('fa-bell', '#5e6e8a', '#f1f5f9'))
        notif.icon_class = meta[0]
        notif.icon_color = meta[1]
        notif.icon_bg    = meta[2]

    # ── Role detection for sidebar ───────────────────────────
    try:
        profile      = request.user.employeeprofile
        is_it_head   = (profile.department == 'IT' and profile.is_department_head)
        is_dept_head = (profile.is_department_head and profile.department != 'IT')
        is_it_staff  = (profile.department == 'IT' and not profile.is_department_head)
    except EmployeeProfile.DoesNotExist:
        profile = None
        is_it_head = is_dept_head = is_it_staff = False

    # ── Build base query string (for pagination links) ───────
    params_copy = request.GET.copy()
    params_copy.pop('page', None)
    base_qs_str = params_copy.urlencode()
    
    _ROLE_NOTIF_TYPES = {
        'it_head':   {
            'needs_assigning', 'ticket_reopened', 'pm_finished',
            'it_resolved', 'rating_received',
        },
        'dept_head': {
            'needs_approval', 'ticket_dispatched', 'ticket_resolved',
        },
        'it_staff':  {
            'ticket_assigned', 'pm_announced', 'feedback_received',
            'ticket_escalated',
        },
        'employee':  {
            'ticket_approved', 'ticket_rejected', 'ticket_resolved',
        },
    }
    
    if is_it_head:
        _relevant = _ROLE_NOTIF_TYPES['it_head']
    elif is_dept_head:
        _relevant = _ROLE_NOTIF_TYPES['dept_head']
    elif is_it_staff:
        _relevant = _ROLE_NOTIF_TYPES['it_staff']
    else:
        _relevant = _ROLE_NOTIF_TYPES['employee']

    notif_types = [
        (code, label)
        for code, label in Notification.NOTIF_TYPE_CHOICES
        if code in _relevant
    ]

    

    context = {
        'page_obj':       page_obj,
        'filter_type':    filter_type,
        'type_filter':    type_filter,
        'all_count':      all_count,
        'unread_count':   unread_count,
        'read_count':     read_count,
        'profile':        profile,
        'is_it_head':     is_it_head,
        'is_dept_head':   is_dept_head,
        'is_it_staff':    is_it_staff,
        'notif_types':    Notification.NOTIF_TYPE_CHOICES,
        'base_qs_str':    base_qs_str,
    }
    return render(request, 'tickets/notifications_inbox.html', context) 


@login_required
def read_and_redirect_notification(request, notif_id):
    """Marks a single notification as read and routes the user to the
    correct page — based on their ROLE first, notification type second."""
 
    notif = get_object_or_404(Notification, id=notif_id, recipient=request.user)
 
    # 1. Mark as read
    if not notif.is_read:
        notif.is_read = True
        notif.save()
 
    # 2. Get profile (fallback-safe)
    try:
        profile = request.user.employeeprofile
    except EmployeeProfile.DoesNotExist:
        return redirect('dashboard_redirect')
 
    ntype = notif.notif_type
 
    # 3. Classify the current user's role
    is_it_head   = (profile.department == 'IT' and profile.is_department_head)
    is_dept_head = (profile.is_department_head and profile.department != 'IT')
    is_it_staff  = (profile.department == 'IT' and not profile.is_department_head)
    # Anything else → regular employee
 
    # ── IT HEAD ─────────────────────────────────────────────────
    # IT Heads are routed to their portal for EVERY notification,
    # even if the notif_type is one that also goes to employees/dept heads.
    if is_it_head:
        if ntype == 'pm_finished':
            url = reverse('it_head_dashboard') + '?view=maintenance_review'
        elif ntype in ('it_resolved', 'rating_received'):
            url = reverse('it_head_dashboard') + '?view=history'
        elif ntype in ('needs_assigning', 'ticket_reopened', 'ticket_escalated'):
            url = reverse('it_head_dashboard')
        else:
            # Any other notif an IT Head might receive → home dashboard
            url = reverse('it_head_dashboard_home')
 
    # ── DEPT HEAD (non-IT) ───────────────────────────────────────
    # Dept Heads are routed to their manager portal for every notif,
    # including ticket_resolved (which used to send them to clinic_portal).
    elif is_dept_head:
        if ntype == 'needs_approval':
            url = reverse('head_dashboard')                      # Pending Approvals tab
        elif ntype in ('ticket_dispatched', 'ticket_resolved'):
            url = reverse('head_dashboard') + '?view=history'   # All Dept Tickets tab
        else:
            # Any unexpected notif type → their home overview
            url = reverse('head_dashboard_home')
 
    # ── IT STAFF ────────────────────────────────────────────────
    elif is_it_staff:
        if ntype == 'pm_announced':
            url = reverse('it_staff_dashboard') + '?view=maintenance'
        elif ntype in ('feedback_received',):
            url = reverse('it_staff_dashboard') + '?view=history'
        elif ntype in ('ticket_assigned', 'ticket_escalated'):
            url = reverse('it_staff_dashboard')                  # Active Tasks tab
        else:
            url = reverse('it_staff_dashboard_home')
 
    # ── REGULAR EMPLOYEE ─────────────────────────────────────────
    else:
        # ticket_approved, ticket_rejected, ticket_resolved → My Tickets
        url = reverse('clinic_portal')
 
    return redirect(url)

@login_required
def head_create_ticket(request):
    """
    Department Heads submit tickets that bypass the approval stage entirely.
    The ticket is created with needs_head_approval=False and
    approval_status='NOT_REQUIRED', then IT Heads are notified immediately
    so they can assign it to an IT staff member.
    """
    try:
        profile = request.user.employeeprofile
        if not profile.is_department_head or profile.department == 'IT':
            return HttpResponseForbidden("Only non-IT department heads may use this form.")
    except EmployeeProfile.DoesNotExist:
        return HttpResponseForbidden("Profile not found.")
 
    if request.method == 'POST':
        form = TicketForm(request.POST)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.category            = request.POST.get('category', 'OTHER')
            ticket.requester           = request.user
            ticket.department          = profile.department
            ticket.priority            = 'MEDIUM'  # Default; IT Head triages priority after assignment
            # ── KEY DIFFERENCE: skip approval, send straight to IT ──
            ticket.needs_head_approval = False
            ticket.approval_status     = 'NOT_REQUIRED'
            ticket.ticket_number       = _generate_ticket_number()
            ticket.save()
 
            # Notify every IT Head so they can assign the ticket
            for it_head in _it_head_users():
                push_notification(
                    recipient=it_head,
                    notif_type='needs_assigning',
                    title='New Ticket — Needs Assignment',
                    message=(
                        f'{request.user.get_full_name() or request.user.username} '
                        f'({profile.get_department_display()} Head) submitted '
                        f'"{ticket.title}" and it is ready to be assigned to IT staff.'
                    ),
                    ticket=ticket,
                )
 
            messages.success(
                request,
                "Your request has been submitted and sent directly to the IT team for assignment."
            )
            return redirect('head_dashboard')
 
    else:
        form = TicketForm(initial={
            'client_name': request.user.get_full_name() or request.user.username,
        })
 
    categories = TicketCategory.objects.filter(is_active=True).order_by('sort_order', 'label')
    return render(request, 'tickets/head_create_ticket.html', {'form': form, 'categories': categories})