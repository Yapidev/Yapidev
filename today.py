import datetime
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dateutil import relativedelta

try:
    import requests
except ImportError:
    requests = None

try:
    from lxml import etree as ET
    USING_LXML = True
except ImportError:
    import xml.etree.ElementTree as ET
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    USING_LXML = False

# Fine-grained personal access token (optional — falls back to GITHUB_TOKEN for public repos):
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Metadata
ACCESS_TOKEN = os.environ['ACCESS_TOKEN']
USER_NAME = os.environ.get('USER_NAME', 'Yapidev')
HEADERS = {'Authorization': f'token {ACCESS_TOKEN}', 'User-Agent': 'Yapidev-README-Bot'}

# Optional: set your birthday here (e.g. datetime.datetime(2004, 5, 12)) to display your age as Uptime.
# Leave as None to track your GitHub account uptime automatically.
BIRTHDAY = None

QUERY_COUNT = {
    'user_getter': 0,
    'follower_getter': 0,
    'graph_repos_stars': 0,
    'recursive_loc': 0,
    'graph_commits': 0,
    'loc_query': 0,
}


def format_plural(unit):
    return 's' if unit != 1 else ''


def daily_readme(start_date):
    """
    Returns elapsed time since start_date, e.g. '5 years, 3 months, 15 days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), start_date)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (BIRTHDAY is not None and diff.months == 0 and diff.days == 0) else '',
    )


_SESSION = requests.Session() if requests is not None else None


def graphql_post(query, variables):
    payload = {'query': query, 'variables': variables}
    last_err = None
    for attempt in range(4):
        try:
            if _SESSION is not None:
                resp = _SESSION.post('https://api.github.com/graphql', json=payload, headers=HEADERS, timeout=30)
                if resp.status_code in (502, 503, 504) and attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return resp.status_code, resp.text, (resp.json() if resp.status_code == 200 else None)

            req = urllib.request.Request(
                'https://api.github.com/graphql',
                data=json.dumps(payload).encode('utf-8'),
                headers={**HEADERS, 'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode('utf-8')
                return resp.status, raw, json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', errors='replace')
            if e.code in (502, 503, 504) and attempt < 3:
                time.sleep(1.5 * (attempt + 1))
                continue
            return e.code, raw, None
        except Exception as e:
            last_err = e
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last_err


def simple_request(func_name, query, variables):
    status, text, parsed = graphql_post(query, variables)
    if status == 200 and parsed and 'errors' not in parsed:
        return parsed
    raise Exception(func_name, 'has failed with', status, text, QUERY_COUNT)


def query_count(funct_id):
    QUERY_COUNT[funct_id] += 1


def graph_repos_stars(count_type, owner_affiliation, cursor=None, total_stars=0):
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    data = simple_request(graph_repos_stars.__name__, query, variables)['data']['user']['repositories']
    if count_type == 'repos':
        return data['totalCount']
    total_stars += sum(edge['node']['stargazers']['totalCount'] for edge in data['edges'])
    if data['pageInfo']['hasNextPage']:
        return graph_repos_stars(count_type, owner_affiliation, data['pageInfo']['endCursor'], total_stars)
    return total_stars


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    status, text, parsed = graphql_post(query, variables)
    if status == 200 and parsed and 'data' in parsed and parsed['data']['repository'] is not None:
        branch_ref = parsed['data']['repository']['defaultBranchRef']
        if branch_ref is not None:
            return loc_counter_one_repo(
                owner, repo_name, data, cache_comment,
                branch_ref['target']['history'],
                addition_total, deletion_total, my_commits,
            )
        return 0, 0, 0
    force_close_file(data, cache_comment)
    if status == 403:
        raise Exception("Too many requests in a short amount of time! Hit GitHub anti-abuse limit.")
    raise Exception('recursive_loc() has failed with', status, text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    for node in history['edges']:
        if node['node']['author'] and node['node']['author']['user'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if not history['edges'] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    return recursive_loc(
        owner, repo_name, data, cache_comment,
        addition_total, deletion_total, my_commits,
        history['pageInfo']['endCursor'],
    )


def loc_query(owner_affiliation, comment_size=7, force_cache=False, cursor=None, edges=None):
    if edges is None:
        edges = []
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    repos = simple_request(loc_query.__name__, query, variables)['data']['user']['repositories']
    edges = edges + repos['edges']
    if repos['pageInfo']['hasNextPage']:
        return loc_query(owner_affiliation, comment_size, force_cache, repos['pageInfo']['endCursor'], edges)
    return cache_builder(edges, comment_size, force_cache)


def cache_filename():
    os.makedirs('cache', exist_ok=True)
    return 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    cached = True
    filename = cache_filename()
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = ['This line is a comment block. Write whatever you want here.\n'] * comment_size
        with open(filename, 'w', encoding='utf-8') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r', encoding='utf-8') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index, edge in enumerate(edges):
        repo_hash, commit_count, *_ = data[index].split()
        expected_hash = hashlib.sha256(edge['node']['nameWithOwner'].encode('utf-8')).hexdigest()
        if repo_hash == expected_hash:
            try:
                total_commits = edge['node']['defaultBranchRef']['target']['history']['totalCount']
                if int(commit_count) != total_commits:
                    owner, repo_name = edge['node']['nameWithOwner'].split('/')
                    add_c, del_c, my_c = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = f"{repo_hash} {total_commits} {my_c} {add_c} {del_c}\n"
            except (TypeError, KeyError):
                data[index] = f"{repo_hash} 0 0 0 0\n"

    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(cache_comment)
        f.writelines(data)

    for line in data:
        parts = line.split()
        loc_add += int(parts[3])
        loc_del += int(parts[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    with open(filename, 'r', encoding='utf-8') as f:
        header = f.readlines()[:comment_size] if comment_size > 0 else []
    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(header)
        for edge in edges:
            repo_hash = hashlib.sha256(edge['node']['nameWithOwner'].encode('utf-8')).hexdigest()
            f.write(f"{repo_hash} 0 0 0 0\n")


def force_close_file(data, cache_comment):
    filename = cache_filename()
    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print(f'Partial cache data saved to {filename}.')


def commit_counter(comment_size):
    total_commits = 0
    with open(cache_filename(), 'r', encoding='utf-8') as f:
        data = f.readlines()[comment_size:]
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    query_count('user_getter')
    query = '''
    query($login: String!) {
        user(login: $login) {
            id
            createdAt
        }
    }'''
    user = simple_request(user_getter.__name__, query, {'login': username})['data']['user']
    return {'id': user['id']}, user['createdAt']


def follower_getter(username):
    query_count('follower_getter')
    query = '''
    query($login: String!) {
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    return int(simple_request(follower_getter.__name__, query, {'login': username})['data']['user']['followers']['totalCount'])


def make_dots(dot_width):
    """
    Returns dot padding occupying exactly `dot_width` characters.
    """
    if dot_width <= 0:
        return ''
    if dot_width == 1:
        return ' '
    if dot_width == 2:
        return '. '
    return ' ' + ('.' * (dot_width - 2)) + ' '


def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = str(new_text)


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Updates SVG stats while keeping every row aligned to 60 characters
    and keeping the '|' divider aligned at column 36.
    """
    tree = ET.parse(filename)
    root = tree.getroot()

    age_str = str(age_data)
    repo_str = f"{repo_data:,}" if isinstance(repo_data, int) else str(repo_data)
    contrib_str = f"{contrib_data:,}" if isinstance(contrib_data, int) else str(contrib_data)
    star_str = f"{star_data:,}" if isinstance(star_data, int) else str(star_data)
    commit_str = f"{commit_data:,}" if isinstance(commit_data, int) else str(commit_data)
    follower_str = f"{follower_data:,}" if isinstance(follower_data, int) else str(follower_data)
    loc_add_str, loc_del_str, loc_net_str = [str(x) for x in loc_data]

    # Row: ". Uptime:" (9) + dots + age_str = 60
    find_and_replace(root, 'age_data', age_str)
    find_and_replace(root, 'age_data_dots', make_dots(60 - 9 - len(age_str)))

    # Row: ". Repos:" (8) + repo_dots + repo + " {Contributed: " (15) + contrib + "} " (2) = 36
    #      "| Stars:" (8) + star_dots + star = 24  (total = 60)
    find_and_replace(root, 'repo_data', repo_str)
    find_and_replace(root, 'contrib_data', contrib_str)
    find_and_replace(root, 'repo_data_dots', make_dots(36 - 8 - 15 - 2 - len(repo_str) - len(contrib_str)))
    find_and_replace(root, 'star_data', star_str)
    find_and_replace(root, 'star_data_dots', make_dots(24 - 8 - len(star_str)))

    # Row: ". Commits:" (10) + commit_dots + commit + " " (1) = 36
    #      "| Followers:" (12) + follower_dots + follower = 24  (total = 60)
    find_and_replace(root, 'commit_data', commit_str)
    find_and_replace(root, 'commit_data_dots', make_dots(36 - 10 - 1 - len(commit_str)))
    find_and_replace(root, 'follower_data', follower_str)
    find_and_replace(root, 'follower_data_dots', make_dots(24 - 12 - len(follower_str)))

    # Row: ". Lines of Code:" (16) + loc_dots + loc_net + " ( " (3) + loc_add + "++, " (4) + loc_del + "-- )" (4) = 60
    find_and_replace(root, 'loc_data', loc_net_str)
    find_and_replace(root, 'loc_add', loc_add_str)
    find_and_replace(root, 'loc_del', loc_del_str)
    loc_dot_w = max(1, 60 - 16 - 3 - 4 - 4 - len(loc_net_str) - len(loc_add_str) - len(loc_del_str))
    find_and_replace(root, 'loc_data_dots', make_dots(loc_dot_w))

    tree.write(filename, encoding='utf-8', xml_declaration=True)


def perf_counter(funct, *args):
    start = time.perf_counter()
    result = funct(*args)
    return result, time.perf_counter() - start


def formatter(query_type, difference):
    unit = f'{difference:.4f} s' if difference > 1 else f'{difference * 1000:.4f} ms'
    print(f'   {query_type + ":":<22} {unit:>12}')


if __name__ == '__main__':
    print('Calculation times:')
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)

    uptime_start = BIRTHDAY if BIRTHDAY is not None else datetime.datetime.strptime(acc_date[:10], '%Y-%m-%d')
    age_data, age_time = perf_counter(daily_readme, uptime_start)
    formatter('uptime calculation', age_time)

    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)' if total_loc[-1] else 'LOC (no cache)', loc_time)

    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    formatted_loc = [f'{x:,}' for x in total_loc[:-1]]

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, formatted_loc)
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, formatted_loc)

    total_time = user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time + follower_time
    print(f'Total function time:      {total_time:>9.4f} s')
    print(f'Total GitHub GraphQL API calls: {sum(QUERY_COUNT.values())}')
    for funct_name, count in QUERY_COUNT.items():
        print(f'   {funct_name + ":":<25} {count:>6}')
