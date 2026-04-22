from django import template

register = template.Library()


@register.simple_tag
def step_nav_class(step, current_task=None):
    if step.is_done:
        return 'step-completed'
    if current_task and step.key == current_task:
        return 'step-active'
    return 'step-upcoming'
