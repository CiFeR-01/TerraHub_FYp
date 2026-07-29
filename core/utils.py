import re

def generate_next_code(model_class, field_name, prefix, default_num=1001, pad=4):
    """
    Generates an automated unique ID like SO-1004, PO-5003, RUN-809, PRD-1001, MAT-1001.
    Searches existing database records for codes, parses the highest trailing integer matching prefix, and increments by 1.
    """
    existing_codes = model_class.objects.values_list(field_name, flat=True)
    max_num = 0
    for code in existing_codes:
        if not code:
            continue
        nums = re.findall(r'\d+', str(code))
        if nums:
            num = int(nums[-1])
            if num > max_num:
                max_num = num
    
    if max_num < default_num - 1:
        next_num = default_num
    else:
        next_num = max_num + 1
        
    candidate = f"{prefix}-{next_num:0{pad}d}" if pad else f"{prefix}-{next_num}"
    while model_class.objects.filter(**{field_name: candidate}).exists():
        next_num += 1
        candidate = f"{prefix}-{next_num:0{pad}d}" if pad else f"{prefix}-{next_num}"
    return candidate
