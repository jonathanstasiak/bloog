# The MIT License
# 
# Copyright (c) 2008 William T. Katz
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""A simple RESTful blog/homepage app for Google App Engine

This simple homepage application tries to follow the ideas put forth in the
book 'RESTful Web Services' by Leonard Richardson & Sam Ruby.  It follows a
Resource-Oriented Architecture where each URI specifies a resource that
accepts HTTP verbs.

Rather than create new URIs to handle web-based form submission of resources,
this app embeds form submissions through javascript.  The ability to send
HTTP verbs POST, PUT, and DELETE is delivered through javascript within the
GET responses.  In other words, a rich client gets transmitted with each GET.

This app's API should be reasonably clean and easily targeted by other clients,
like a Flex app or a desktop program.
"""
__author__ = 'William T. Katz'

import datetime
import string
import re
import os
import urllib

import logging

from google.appengine.ext import webapp
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import search

from external.libs import textile

import restful
import authorized
import model
import view
import config

import legacy_aliases   # This can be either manually created or autogenerated using the drupal_uploader utility

# Functions to generate permalinks depending on type of article
permalink_funcs = {
    'page': lambda title,date: get_friendly_url(title),
    'blog': lambda title,date: str(date.year) + "/" + str(date.month) + "/" + get_friendly_url(title)
}

# Module methods to handle incoming data
def get_datetime(time_string):
    if time_string:
        return datetime.datetime.strptime(time_string, '%Y-%m-%d %H:%M:%S')
    return datetime.datetime.now()
    
def get_tags(tag_string):
    if tag_string:
        return [db.Category(s.strip()) for s in tag_string.split(",") if s != '']
    return None
    
def get_friendly_url(title):
    return re.sub('-+', '-', re.sub('[^\w-]', '', re.sub('\s+', '-', title.strip())))

def get_html(body, markup_type):
    if markup_type == 'textile':
        return textile.textile(str(body))
    return body
    
def fill_optional_properties(obj, property_dict):
    for key, value in property_dict.items():
        if value and not obj.__dict__.has_key(key):
            setattr(obj, key, value)

def process_article_submission(handler, article_type):
    property_hash = restful.get_hash_from_request(handler.request, 
        ['title',
         'body',
         'format',
         'legacy_id',
         ('published', get_datetime),
         ('updated', get_datetime),
         ('tags', get_tags),
         ('html', get_html, 'body', 'format'),
         ('permalink', permalink_funcs[article_type], 'title', 'published')
        ])
    
    article = model.Article(
        permalink = property_hash['permalink'],
        article_type = article_type,
        title = property_hash['title'],
        body = property_hash['body'],
        html = property_hash['html'],
        published = property_hash['published'],
        updated = property_hash['updated'],
        format = 'html'     # We are converting everything to HTML from Drupal since it can mix formats within articles
    )
    fill_optional_properties(article, property_hash)
    article.set_associated_data({'relevant_links': handler.request.get('relevant_links'), 'amazon_items': handler.request.get('amazon_items')})
    article.put()
    restful.successful_post_response(handler, article.permalink, article_type)
    view.invalidate_cache()

def process_comment_submission(handler, article):
    if not article:
        handler.error(404)
        return

    # Get and store some pieces of information from parent article.
    # TODO: See if this overhead can be avoided, perhaps by keeping comments with article.
    if not article.num_comments:
        article.num_comments = 1
    else:
        article.num_comments += 1
    article_key = article.put()

    property_hash = restful.get_hash_from_request(handler.request, 
        ['name',
         'email',
         'title',
         'body',
         'thread',
         ('published', get_datetime)
        ])

    # Compute a comment key by hashing name, email, and body.  If these aren't different, don't bother adding comment.
    comment_key = str(hash((property_hash['name'], property_hash['email'], property_hash['body'])))

    comment = model.Comment(
        permalink = comment_key,
        body = property_hash['body'],
        article = article_key,
        thread = property_hash['thread']
    )
    fill_optional_properties(comment, property_hash)
    comment.put()
    restful.successful_post_response(handler, comment.permalink, 'comment')
    view.invalidate_cache()

class NotFoundHandler(webapp.RequestHandler):
    def get(self):
        view.ViewPage(cache_time=36000).render(self)

class UnauthorizedHandler(webapp.RequestHandler):
    def get(self):
        view.ViewPage(cache_time=36000).render(self)

class RootHandler(restful.Controller):
    def get(self):
        logging.debug("RootHandler#get")
        page = view.ViewPage()
        page.render_query(
            self, 'articles', 
            db.Query(model.Article).filter('article_type =', 'blog').order('-published')
        )

    @authorized.role("admin")
    def post(self):
        logging.debug("RootHandler#post")
        process_article_submission(handler=self, article_type='page')

class PageHandler(restful.Controller):
    def get(self, path):
        logging.debug("PageHandler#get on path (%s)", path)
        # Handle legacy aliases
        for alias in legacy_aliases.redirects:
            if path.lower() == alias.lower():
                self.redirect('/' + legacy_aliases.redirects[alias])
                return

        # Check legacy_id_mapping if it's provided
        article = None
        if config.blog.has_key('legacy_id_mapping'):
            url_match = re.match(config.blog['legacy_id_mapping']['regex'], path)
            if url_match:
                article = config.blog['legacy_id_mapping']['query'](url_match.group(1)).get()

        # Check undated pages
        if not article:
            article = db.Query(model.Article).filter('permalink =', path).get()

        comments = []
        if article:
            for comment in article.comment_set:
                comments.append(comment)

            page = view.ViewPage()
            use_two_columns = article.is_big() or len(article.html) + len(comments)*80 > 2000
            page.render(self, {"two_columns": use_two_columns, "title": article.title, "article": article, "comments": comments})
            return

        # This didn't fall into any of our pages or aliases.
        # Page not found.
        self.redirect('/404.html')

    @authorized.role("user")
    def post(self, path):
        article = db.Query(model.Article).filter('permalink =', path).get()
        process_comment_submission(self, article)

    @authorized.role("admin")
    def delete(self, path):
        """
        By using DELETE on /article or /comment, you can delete the first entity of the desired kind.
        This is useful for writing utilities like clear_datastore.py.  
        TODO - Once we write a DELETE for specific entities, it makse sense to DRY this up and just 
        require a utility to inquire which entities are available and then call DELETE on each permalink.
        """
        model_class = path.lower()

        def delete_entity(query):
            targets = query.fetch(limit=1)
            if len(targets) > 0:
                permalink = targets[0].permalink
                logging.debug('Deleting %s %s', model_class, permalink)
                targets[0].delete()
                self.response.out.write('Deleted ' + permalink)
                view.invalidate_cache()
            else:
                self.error(404)

        if model_class == 'article':
            query = model.Article.all()
            delete_entity(query)
        elif model_class == 'comment':
            query = model.Comment.all()
            delete_entity(query)
        else:
            self.error(404)

class TagHandler(restful.Controller):
    def get(self, encoded_tag):
        tag =  re.sub('(%25|%)(\d\d)', lambda cmatch: chr(string.atoi(cmatch.group(2), 16)), encoded_tag)   # No urllib.unquote in AppEngine?
        logging.debug("TagHandler#get called on uri %s (%s -> %s)", self.request.url, encoded_tag, tag)
        
        page = view.ViewPage()
        page.render_query(
            self, 'articles', 
            db.Query(model.Article).filter('tags =', tag).order('-published'), 
            {'tag': tag}
        )

class SearchHandler(restful.Controller):
    def get(self):
        search_term = self.request.get("s")
        page = view.ViewPage()
        page.render_query(
            self, 'articles', 
            # model.Article.all().search(search_term).order('-published'), 
            db.Query(model.Article).filter('tags =', search_term).order('-published'), 
            {'search_term': search_term}
        )

class YearHandler(restful.Controller):
    def get(self, year):
        logging.debug("YearHandler#get for year %s", year)
        start_date = datetime.datetime(string.atoi(year), 1, 1)
        end_date = datetime.datetime(string.atoi(year), 12, 31, 23, 59, 59)
        page = view.ViewPage()
        page.render_query(
            self, 'articles', 
            db.Query(model.Article).order('-published').filter('published >=', start_date).filter('published <=', end_date), 
            {'title': 'Articles for ' + year, 'year': year}
        )

class MonthHandler(restful.Controller):
    def get(self, year, month):
        logging.debug("MonthHandler#get for year %s, month %s", year, month)
        start_date = datetime.datetime(string.atoi(year), string.atoi(month), 1)
        end_date = datetime.datetime(string.atoi(year), string.atoi(month), 31, 23, 59, 59)
        page = view.ViewPage()
        page.render_query(
            self, 'articles', 
            db.Query(model.Article).order('-published').filter('published >=', start_date).filter('published <=', end_date), 
            {'title': 'Articles for ' + month + '/' + year, 'year': year, 'month': month}
        )

    def post(self, year, month):
        """ Add a blog entry. Since we are POSTing, the server handles creation of the permalink url. """
        logging.debug("MonthHandler#post on date %s, %s", year, month)
        process_article_submission(handler=self, article_type='blog')
        
class ArticleHandler(restful.Controller):
    def get(self, year, month, perm_stem):
        logging.debug("ArticleHandler#get for year %s, month %s, and perm_link %s", year, month, perm_stem)
        article = db.Query(model.Article).filter('permalink =', year + '/' + month + '/' + perm_stem).get()
        comments = []
        if article:
            for comment in article.comment_set:
                logging.debug("Found comment '%s'", comment.title)
                comments.append(comment)

        page = view.ViewPage()
        use_two_columns = article.is_big() or len(article.html) + len(comments)*80 > 2000
        page.render(self, {"two_columns": use_two_columns, "title": article.title, "article": article, "comments": comments})

    @authorized.role("admin")
    def put(self, year, month, perm_stem):
        # TODO: Edit article
        view.invalidate_cache()

    @authorized.role("admin")
    def delete(self, year, month, perm_stem):
        # TODO: Delete this article
        view.invalidate_cache()

    @authorized.role("user")
    def post(self, year, month, perm_stem):
        logging.debug("Adding comment for article %s", self.request.path)
        permalink = year + '/' + month + '/' + perm_stem
        article = db.Query(model.Article).filter('permalink =', permalink).get()
        process_comment_submission(self, article)

class AtomHandler(webapp.RequestHandler):
    def get(self):
        logging.debug("Sending Atom feed")
        articles = db.Query(model.Article).filter('article_type =', 'blog').order('-published').fetch(limit=10)
        updated = ''
        if articles:
            updated = articles[0].rfc3339_updated()
        
        self.response.headers['Content-Type'] = 'application/atom+xml'
        page = view.ViewPage()
        page.render(self, {"blog_updated_timestamp": updated, "articles": articles, "ext": "xml"})
